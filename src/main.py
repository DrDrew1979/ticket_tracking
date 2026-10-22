"""Точка входа мониторинга.

Режимы:
  --mode monitor : сбор цен + мгновенные алерты (каждые 30 мин по cron)
  --mode digest  : ежедневная сводка с трендом и датой отсечки (1 раз в день)

Секреты берутся из окружения: TP_TOKEN, TG_TOKEN, TG_CHAT_ID.
"""
import argparse
import logging
import os
import time
from datetime import date

from .config import date_pairs, load_config
from .cutoff import cutoff_status
from .db import DB
from .fetcher import TPClient
from .notify import alert_text, digest_text, send_text
from .rules import evaluate_pair
from .trends import trend_verdict

log = logging.getLogger("monitor")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(HERE), "prices.db")


def _mark_commit():
    """Сигнализирует workflow, что prices.db изменился и нужен коммит."""
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as f:
            f.write("NEED_COMMIT=1\n")


def run_monitor(cfg: dict) -> int:
    token = os.environ.get("TP_TOKEN")
    if not token:
        log.error("TP_TOKEN не задан — добавьте его в Secrets репозитория")
        return 2

    client = TPClient(token, marker=cfg.get("marker") or None)
    db = DB(DB_PATH)
    total_alerts = 0
    tg_token = os.environ.get("TG_TOKEN")
    tg_chat = os.environ.get("TG_CHAT_ID")

    try:
        for depart, ret in date_pairs(cfg):
            alerts = evaluate_pair(cfg, db, client, depart, ret)
            for a in alerts:
                total_alerts += 1
                log.info("АЛЕРТ min30: %s -> %s за %s RUB",
                         a["depart"], a["ret"], a["price"])
                if tg_token and tg_chat:
                    send_text(tg_token, tg_chat, alert_text(a, cfg))
                _mark_commit()
            time.sleep(1.5)  # вежливая пауза между запросами к API
    finally:
        db.close()
    log.info("Готово. Алертов за прогон: %d", total_alerts)
    return 0


def run_digest(cfg: dict) -> int:
    token = os.environ.get("TP_TOKEN")
    tg_token = os.environ.get("TG_TOKEN")
    tg_chat = os.environ.get("TG_CHAT_ID")
    if not (token and tg_token and tg_chat):
        log.error("Нужны TP_TOKEN, TG_TOKEN и TG_CHAT_ID в Secrets")
        return 2

    client = TPClient(token, marker=cfg.get("marker") or None)
    db = DB(DB_PATH)

    pairs_now = []
    best_series = []
    try:
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
                    "link": offers[0].get("link", ""),
                })
            time.sleep(1.5)

        if pairs_now:
            best = min(pairs_now, key=lambda x: x["price"])
            best_series = db.daily_min_series(best["depart"].isoformat(),
                                              best["ret"].isoformat(), 30)
        text = digest_text(cfg, pairs_now, best_series)
        _mark_commit()  # дайджест тоже сохраняет наблюдения
    finally:
        db.close()

    ok = send_text(tg_token, tg_chat, text)
    log.info("Дайджест отправлен: %s", ok)
    return 0


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["monitor", "digest"], default="monitor")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    rc = run_monitor(cfg) if args.mode == "monitor" else run_digest(cfg)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
