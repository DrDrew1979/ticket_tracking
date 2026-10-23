"""Точка входа. Режимы:

  --mode monitor : каждые 30 мин по cron:
                   1) приём команд Telegram (getUpdates, без сервера);
                   2) личный мониторинг владельца (config.yaml);
                   3) мониторинг трекингов пользователей бота
                      (частота: владелец — каждый прогон, остальные —
                      не чаще раза в час; глобальный бюджет пар).
  --mode digest  : ежедневный дайджест владельца (06:00 UTC = 09:00 МСК).
  --mode udigest : ежечасно — персональные дайджесты пользователей,
                   подошедшие по их личному расписанию.

Секреты: TP_TOKEN, TG_TOKEN, TG_CHAT_ID (адресат личного дайджеста
владельца; можно опустить — тогда дайджест и финал уйдут на
ADMIN_CHAT_ID), DB_KEY (Fernet-ключ шифрования персональных данных),
ADMIN_CHAT_ID (админ; по умолчанию = TG_CHAT_ID),
опционально HC_URL (маячок healthchecks.io).

Без DB_KEY мультипользовательский режим отключается (лог-предупреждение),
личный мониторинг владельца продолжает работать.
"""
import argparse
import logging
import os
from datetime import date

from .config import date_pairs, load_config, stop_date
from .db import DB, OWNER
from .fetcher import TPClient
from .healthcheck import ping
from .notify import digest_text, send_text

log = logging.getLogger("monitor")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(HERE), "prices.db")
STOP_KEY = "stopped"
DBKEY_FLAG = "dbkey_broken"  # meta-флаг «DB_KEY задан, но битый»


def _mark_commit():
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write("NEED_COMMIT=1\n")


def _hc_url(cfg) -> str:
    hc = cfg.get("healthcheck") or {}
    if not hc.get("enabled", False):
        return ""
    return os.environ.get("HC_URL") or hc.get("url") or ""


def _owner_chat() -> str:
    """Адресат ЛИЧНЫХ сообщений владельца (дайджест, финал мониторинга).

    TG_CHAT_ID, а если он не задан — ADMIN_CHAT_ID (роли владельца и
    администратора обычно у одного человека, поэтому дублировать chat_id
    в двух секретах не обязательно).
    """
    return (os.environ.get("TG_CHAT_ID")
            or os.environ.get("ADMIN_CHAT_ID") or "")


def _final_text(cfg) -> str:
    last = max(date.fromisoformat(cfg["return_window"]["date_from"]),
               date.fromisoformat(cfg["return_window"]["date_to"]))
    return ("🏁 <b>Личный мониторинг завершён</b>: даты прошли "
            f"(последняя — {last:%d.%m.%Y}).")


def _check_stop(cfg: dict, db: DB) -> bool:
    sd = stop_date(cfg)
    if date.today() <= sd:
        if db.get_meta(STOP_KEY) == "1":
            db.set_meta(STOP_KEY, "0")
            log.info("Даты сдвинуты (остановка теперь %s) — возобновляем", sd)
        return False
    if db.get_meta(STOP_KEY) != "1":
        token, chat = os.environ.get("TG_TOKEN"), _owner_chat()
        if token and chat:
            send_text(token, chat, _final_text(cfg))
        db.set_meta(STOP_KEY, "1")
    log.info("Личный мониторинг остановлен (дата %s)", sd)
    return True


def _crypto(cfg):
    key = os.environ.get("DB_KEY")
    if not key:
        log.warning("DB_KEY не задан — мультипользовательский режим "
                    "выключен (личный мониторинг работает)")
        return None
    try:
        from .crypto import Crypto
        return Crypto(key)
    except ValueError:
        log.error(
            "DB_KEY задан, но это не корректный Fernet-ключ (нужно 44 "
            "символа url-safe base64, заканчиваются на '='). Ключ нельзя "
            "придумать самому — сгенерируйте командой:\n"
            "  python -c \"import base64, os; "
            "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"\n"
            "и обновите секрет DB_KEY. Мультипользовательский режим "
            "выключен, личный мониторинг владельца продолжит работу.")
        return None


def _dbkey_alert(db: DB, crypto, admin_chat: str):
    """Разовое TG-оповещение админу о битом DB_KEY; сброс флага при починке."""
    if not admin_chat:
        return
    broken = bool(os.environ.get("DB_KEY")) and crypto is None
    if broken and db.get_meta(DBKEY_FLAG) != "1":
        token = os.environ.get("TG_TOKEN")
        if token:
            send_text(token, admin_chat,
                      "⚠️ <b>Секрет DB_KEY задан неверно</b> — бот не принимает "
                      "команды пользователей, личный мониторинг работает.\n\n"
                      "Сгенерируйте ключ:\n<code>python -c \"import base64, os; "
                      "print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
                      "\"</code>\nи обновите секрет DB_KEY в настройках репо.")
        db.set_meta(DBKEY_FLAG, "1")
    elif not broken and db.get_meta(DBKEY_FLAG) == "1":
        db.set_meta(DBKEY_FLAG, "0")  # ключ починили — разрешаем будущие алерты


def run_monitor(cfg: dict) -> int:
    token = os.environ.get("TP_TOKEN")
    if not token:
        log.error("TP_TOKEN не задан — добавьте его в Secrets репозитория")
        return 2
    client = TPClient(token, marker=cfg.get("marker") or None)
    db = DB(DB_PATH)
    hc = _hc_url(cfg)
    try:
        # 1. команды Telegram -> база (шифрование обязательно)
        crypto = _crypto(cfg)
        admin_chat = (os.environ.get("ADMIN_CHAT_ID")
                      or os.environ.get("TG_CHAT_ID") or "")
        _dbkey_alert(db, crypto, admin_chat)
        if crypto and os.environ.get("TG_TOKEN") and admin_chat:
            from . import bot
            try:
                n = bot.poll_and_process(os.environ["TG_TOKEN"], db, crypto,
                                         cfg, admin_chat)
                if n:
                    log.info("Обработано команд/сообщений: %d", n)
            except Exception:
                log.exception("Опрос Telegram упал (не критично)")

        # 2. гигиена базы: retention + еженедельный VACUUM
        mu = cfg.get("multiuser") or {}
        db.purge(int(mu.get("retention_days", 45)))

        # 3. личный мониторинг владельца (останавливается по stop-датe)
        if _check_stop(cfg, db):
            ping(hc)
            return 0
        from . import monitor
        alerts = monitor.run_owner(cfg, db, client)
        log.info("Владелец: алертов за прогон %d", alerts)

        # 4. мониторинг пользователей бота
        if crypto and admin_chat and (mu.get("enabled", True)):
            st = monitor.run_users(cfg, db, client, crypto, admin_chat)
            log.info("Пользователи: опрошено %d, алертов %d, API-вызовов %d, "
                     "истекло %d, калибровок %d",
                     st["checked"], st["alerts"], st["api_calls"],
                     st["expired"], st["calibrated"])
        _mark_commit()
        ping(hc)
        return 0
    except Exception:
        ping(hc, ok=False)
        raise
    finally:
        db.close()


def run_digest(cfg: dict) -> int:
    """Ежедневный дайджест владельца (свежие данные + тренд + отсечка)."""
    token = os.environ.get("TP_TOKEN")
    tg_token = os.environ.get("TG_TOKEN")
    tg_chat = _owner_chat()
    if not (token and tg_token and tg_chat):
        log.error("Нужны TP_TOKEN и TG_TOKEN в Secrets, а также "
                  "TG_CHAT_ID или ADMIN_CHAT_ID (адресат дайджеста)")
        return 2
    client = TPClient(token, marker=cfg.get("marker") or None)
    db = DB(DB_PATH)
    hc = _hc_url(cfg)
    pairs_now, best_series = [], []
    try:
        if _check_stop(cfg, db):
            return 0
        for depart, ret in date_pairs(cfg):
            offers = client.fetch_roundtrip(
                cfg["route"]["origin"], cfg["route"]["destination"],
                depart.isoformat(), ret.isoformat(), cfg.get("currency", "rub"))
            if offers:
                db.store_offers(depart.isoformat(), ret.isoformat(), offers)
                pairs_now.append({
                    "depart": depart, "ret": ret,
                    "price": min(int(o["price"]) for o in offers),
                    "airline": offers[0].get("airline", "?"),
                    "link": offers[0].get("link", "")})
            import time
            time.sleep(1.5)
        if pairs_now:
            best = min(pairs_now, key=lambda x: x["price"])
            best_series = db.daily_min_series(best["depart"].isoformat(),
                                              best["ret"].isoformat(), 30)
        _mark_commit()
        send_text(tg_token, tg_chat, digest_text(cfg, pairs_now, best_series))
        ping(hc)
        return 0
    except Exception:
        ping(hc, ok=False)
        raise
    finally:
        db.close()


def run_udigest(cfg: dict) -> int:
    """Персональные дайджесты пользователей (из БД, без API-запросов)."""
    crypto = _crypto(cfg)
    tg_token = os.environ.get("TG_TOKEN")
    if not (crypto and tg_token):
        log.warning("udigest пропущен: нет DB_KEY или TG_TOKEN")
        return 0
    db = DB(DB_PATH)
    hc = _hc_url(cfg)
    try:
        from . import digest
        admin_chat = (os.environ.get("ADMIN_CHAT_ID")
                      or os.environ.get("TG_CHAT_ID") or "")
        digest.run_user_digests(cfg, db, crypto)
        _mark_commit()
        ping(hc)
        return 0
    except Exception:
        ping(hc, ok=False)
        raise
    finally:
        db.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["monitor", "digest", "udigest"],
                    default="monitor")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)
    rc = {"monitor": run_monitor, "digest": run_digest,
          "udigest": run_udigest}[args.mode](cfg)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
