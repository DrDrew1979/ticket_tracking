"""Циклы мониторинга: владелец (config.yaml) + пользователи бота.

Пользовательская частота (ТЗ): владелец — каждый прогон (*/30 мин),
остальные — не чаще раза в user_min_interval_min (60). Глобальный
бюджет пар за прогон — pairs_budget_per_run (антинагрузочный предохранитель).
"""
import logging
import time
from datetime import date, datetime

from . import rules, users as U
from .rotation import select_pairs

log = logging.getLogger(__name__)
OWNER = "__owner__"


# --------------------------------------------------------------- владелец

def run_owner(cfg, db, client) -> int:
    """Личный мониторинг владельца. Возвращает число алертов."""
    from .notify import alert_text
    import os
    tg_token = os.environ.get("TG_TOKEN")
    tg_chat = os.environ.get("TG_CHAT_ID")
    out_w, ret_w = rules.owner_windows(cfg)
    m = cfg["monitor"]
    pairs, note = select_pairs(cfg, db, rules.tracking_pairs({
        "out": out_w, "ret": ret_w, "oneway": False}))
    log.info("Владелец, этот прогон: %s", note)
    total = 0
    for d, r in pairs:
        alerts = rules.evaluate_pair(
            db, client, OWNER,
            origin=cfg["route"]["origin"], dest=cfg["route"]["destination"],
            d=d, r=r, oneway=False, out_w=out_w, ret_w=ret_w,
            target=int(cfg["price_target_rub"]),
            currency=cfg.get("currency", "rub"),
            marker=cfg.get("marker") or None,
            carry_on=bool(cfg.get("carry_on_only")),
            win_days=int(m.get("history_days_for_min", 30)),
            warmup_days=int(m.get("warmup_days", 2)),
            dedup_hours=int(m.get("alert_dedup_hours", 12)),
            improve_pct=float(m.get("alert_improvement_pct", 0.5)))
        for a in alerts:
            total += 1
            log.info("АЛЕРТ min30 (владелец): %s -> %s за %s RUB",
                     a["depart"], a["ret"], a["price"])
            if tg_token and tg_chat:
                from .notify import send_text
                send_text(tg_token, tg_chat, alert_text(a, cfg))
        time.sleep(1.5)
    return total


# ------------------------------------------------------------- пользователи

def _due(row, admin_u: str, interval_min: int, now: datetime) -> bool:
    if row["uid"] == admin_u:
        return True
    if not row["last_check"]:
        return True
    try:
        last = datetime.fromisoformat(row["last_check"])
    except ValueError:
        return True
    return (now - last).total_seconds() >= interval_min * 60


def run_users(cfg, db, client, crypto, admin_chat: str) -> dict:
    """Мониторинг трекингов пользователей бота. Возвращает статистику."""
    import os
    from .notify import send_text
    tg_token = os.environ.get("TG_TOKEN")
    mu = cfg.get("multiuser") or {}
    admin_u = U.admin_uid(crypto, admin_chat)
    now = datetime.utcnow()
    stats = {"checked": 0, "alerts": 0, "api_calls": 0, "expired": 0,
             "waitlisted": 0, "calibrated": 0}

    # 1. истёкшие трекинги: прощание + полное удаление из базы (по ТЗ)
    for exp in U.expired_users(db, crypto, date.today()):
        if tg_token and exp["chat_id"]:
            send_text(tg_token, exp["chat_id"],
                      f"🏁 {exp['name'] + ', ' if exp['name'] else ''}даты "
                      f"вашего отслеживания прошли (до "
                      f"{exp['ended']:%d.%m.%Y}). Трекинг удалён из бота. "
                      f"Начать новый: /start")
        db.delete_user(exp["uid"])
        stats["expired"] += 1
    if stats["expired"]:
        log.info("Истёкших трекингов удалено: %d", stats["expired"])

    # 2. лист ожидания: уведомляем старейшего, если место свободно
    for row in U.waitlist_notify(db, mu, admin_u):
        ident = crypto.dec(row["ident_enc"]) or {}
        if tg_token and ident.get("chat_id"):
            send_text(tg_token, str(ident["chat_id"]),
                      "🎉 Появилось свободное место! Ваши параметры "
                      "сохранены — активируйте их командой /retry "
                      "(или настройте заново: /watch).")
        db.update_user(row["uid"], wl_notified=1)
        stats["waitlisted"] += 1

    # 3. активные пользователи: калибровка новых + опрос по cadence
    interval = int(mu.get("user_min_interval_min", 60))
    budget = int(mu.get("pairs_budget_per_run", 200))
    m = cfg["monitor"]
    due = [r for r in db.all_users(status="active", with_tracking=True)
           if _due(r, admin_u, interval, now)]
    due.sort(key=lambda r: (r["last_check"] or ""))

    for row in due:
        if budget <= 0:
            log.info("Бюджет пар исчерпан (%d), остальные — в следующий "
                     "прогон", int(mu.get("pairs_budget_per_run", 200)))
            break
        t = crypto.dec(row["tracking_enc"])
        if not t:
            continue

        # разовая калибровка нового трекинга (статистика маршрута)
        if t.get("need_calib"):
            from . import calib
            c = calib.calibrate(client, t["origin"], t["dest"],
                                t["out"]["from"], cfg.get("currency", "rub"))
            if c:
                t["calib"], t["need_calib"] = c, False
                db.update_user(row["uid"],
                               tracking_enc=crypto.enc(t))
                stats["calibrated"] += 1
                stats["api_calls"] += c.get("calls", 0)

        ident = crypto.dec(row["ident_enc"]) or {}
        chat_id = str(ident.get("chat_id", ""))
        pairs = rules.tracking_pairs(t)
        ret_w = t.get("ret") if not t.get("oneway") else None
        checked = 0
        for d, r in pairs:
            if budget <= 0:
                break
            alerts = rules.evaluate_pair(
                db, client, row["uid"],
                origin=t["origin"], dest=t["dest"],
                d=d, r=r, oneway=bool(t.get("oneway")),
                out_w=t["out"], ret_w=ret_w,
                target=int(t.get("target", 1)),
                currency=cfg.get("currency", "rub"),
                marker=cfg.get("marker") or None,
                carry_on=bool(cfg.get("carry_on_only")),
                win_days=int(m.get("history_days_for_min", 30)),
                warmup_days=int(mu.get("warmup_days", 2)),
                dedup_hours=int(mu.get("alert_dedup_hours", 12)),
                improve_pct=float(m.get("alert_improvement_pct", 0.5)))
            checked += 1
            budget -= 1
            stats["api_calls"] += 1
            for a in alerts:
                stats["alerts"] += 1
                log.info("АЛЕРТ min30 (uid %s): %s -> %s за %s RUB",
                         row["uid"][:6], a["depart"], a["ret"], a["price"])
                if tg_token and chat_id:
                    send_text(tg_token, chat_id, _user_alert_text(a, t))
            time.sleep(1.5)
        if checked == len(pairs):  # полностью обработан
            db.update_user(row["uid"],
                           last_check=datetime.utcnow().isoformat(
                               timespec="seconds"))
            stats["checked"] += 1
    return stats


def _user_alert_text(a: dict, t: dict) -> str:
    o = a["offer"]
    tr = o.get("transfers", "?")
    rtr = o.get("return_transfers", "?")
    direction = f"{t.get('origin_name')} → {t.get('dest_name')}"
    if not t.get("oneway"):
        direction += f" → {t.get('origin_name')}"
    return (
        f"✈️ <b>ЛУЧШАЯ ЦЕНА ЗА 30 ДНЕЙ!</b>\n\n"
        f"💰 <b>{a['price']:,}</b> ₽ (цель ≤ {t.get('target'):,} ₽)"
        .replace(",", " ") +
        f"\n🛫 {direction}: {_dt(o.get('departure_at'))}\n"
        + (f"🛬 Обратно: {_dt(o.get('return_at'))}\n" if a.get("ret") else "")
        + f"🔁 Пересадки: {tr}"
        + (f" туда / {rtr} обратно" if a.get("ret") else "")
        + f"\n🛩 {o.get('airline', '?')}\n\n"
        f"👉 <a href=\"{a['link']}\">Проверить и купить на Aviasales</a>\n"
        f"<i>Цена из кэша — на кассе может отличаться. Нажали «купить» — "
        f"покупаете сами.</i>"
    )


def _dt(iso):
    return iso.replace("T", " ")[:16] if iso else "?"
