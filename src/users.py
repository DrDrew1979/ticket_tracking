"""Пользователи бота: регистрация, лимит активных трекингов, статусы.

Лимиты (ТЗ): трекинг может вести неограниченное число зарегистрированных,
но АКТИВНЫХ трекингов у обычных пользователей не более max_active (39).
У администратора всегда есть свой слот (он не входит в 39).
"""
import logging
from datetime import date, datetime, timedelta

from .db import DB

log = logging.getLogger(__name__)


def admin_uid(crypto, admin_chat) -> str:
    return crypto.uid(admin_chat)


def ensure_user(db: DB, crypto, chat_id, from_user: dict | None) -> tuple:
    """Регистрирует/находит пользователя. Возвращает (uid, row, created).

    Забаненный находит свою запись со status='banned' — вызывающий код
    обязан молчать (тихий режим).
    """
    uid = crypto.uid(chat_id)
    row = db.get_user(uid)
    if row:
        return uid, row, False
    ident = {
        "chat_id": int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id,
        "username": (from_user or {}).get("username", ""),
        "first": (from_user or {}).get("first_name", ""),
        "last": (from_user or {}).get("last_name", ""),
    }
    created = db.upsert_user(uid, crypto.enc(ident),
                             date.today().isoformat())
    if created:
        total = int(db.get_meta("users_total_started", "0") or 0) + 1
        db.set_meta("users_total_started", total)
    return uid, db.get_user(uid), created


def tracking_active(t: dict) -> bool:
    return bool(t)


def free_slots(db: DB, cfg_mu: dict, admin_uid_str: str = "") -> int:
    cap = int(cfg_mu.get("max_active", 39))
    used = db.active_tracking_count(exclude_uid=admin_uid_str or None)
    return max(cap - used, 0)


def save_tracking(db: DB, crypto, uid: str, admin_uid_str: str,
                  tracking: dict, cfg_mu: dict) -> str:
    """Активация трекинга. 'ok' | 'waitlist'.

    Трекинг администратора всегда помещается (свой слот вне лимита 39).
    """
    tracking["created"] = date.today().isoformat()
    tracking["need_calib"] = True
    tracking["calib"] = None
    if uid == admin_uid_str:
        db.update_user(uid, tracking_enc=crypto.enc(tracking), waitlist=0,
                       wl_notified=0, status="active")
        return "ok"
    if free_slots(db, cfg_mu, admin_uid_str) > 0:
        db.update_user(uid, tracking_enc=crypto.enc(tracking), waitlist=0,
                       wl_notified=0)
        return "ok"
    db.update_user(uid, waitlist=1, wl_notified=0)  # черновик остаётся в dialog
    log.info("Лимит трекингов исчерпан: uid=%s в листе ожидания", uid)
    return "waitlist"


def remove_tracking(db: DB, crypto, uid: str):
    """Пользователь сам удалил трекинг (запись пользователя остаётся).

    Возвращает прежний трекинг (расшифрованный dict) или None —
    dialog.py сохраняет его в состоянии «stopped», чтобы при повторной
    настройке (/watch) подставлять прежние значения в каждый вопрос.
    """
    row = db.get_user(uid)
    old = None
    if row and row["tracking_enc"]:
        old = crypto.dec(row["tracking_enc"])
    db.update_user(uid, tracking_enc=None, last_check=None, last_digest=None)
    return old


def admin_delete_user(db: DB, crypto, uid: str):
    """Мягкое удаление: запись стирается, пользователь может /start заново."""
    db.delete_user(uid)


def admin_ban(db: DB, crypto, uid: str, banned: bool):
    """Бан: бот молчит (тихий режим). unban возвращает доступ."""
    db.update_user(uid, status="banned" if banned else "active",
                   dialog_enc=None)


def expired_users(db: DB, crypto, today: date | None = None) -> list[dict]:
    """Трекинги, у которых даты прошли (последняя дата окна возврата/вылета).

    По ТЗ пользователь удаляется из базы полностью, но может /start заново.
    Возвращает список для отправки прощального сообщения ПЕРЕД удалением:
    [{uid, chat_id, name, ended}]
    """
    today = today or date.today()
    out = []
    for row in db.all_users(status="active", with_tracking=True):
        t = crypto.dec(row["tracking_enc"])
        if not t:
            continue
        w = t.get("ret") or t.get("out") or {}
        end = date.fromisoformat(w["from"]) + timedelta(days=int(w["days"]) - 1)
        if end < today:
            ident = crypto.dec(row["ident_enc"]) or {}
            out.append({"uid": row["uid"], "chat_id": ident.get("chat_id"),
                        "name": ident.get("first") or ident.get("username") or "",
                        "ended": end})
    return out


def waitlist_notify(db: DB, cfg_mu: dict, admin_uid_str: str = "") -> list:
    """Пользователи листа ожидания, которым пора сообщить о свободном месте."""
    if free_slots(db, cfg_mu, admin_uid_str) <= 0:
        return []
    row = db.oldest_waitlist()
    return [row] if row else []


def stats(db: DB, cfg_mu: dict) -> str:
    started = db.get_meta("users_total_started", "0")
    active = len(db.all_users(status="active"))
    banned = len(db.all_users(status="banned"))
    track_all = db.active_tracking_count()
    cap = int(cfg_mu.get("max_active", 39))
    wl = len(db.all_users(waitlist=1))
    return (
        f"👥 Пользователей: {active} активно / {started} стартовало всего "
        f"({banned} в бане)\n"
        f"🎯 Активных трекингов: {track_all} (лимит {cap}, свободно "
        f"{max(cap - track_all, 0)})\n"
        f"⏳ Лист ожидания: {wl}"
    )


def _ident_line(ident: dict) -> str:
    name = " ".join(x for x in [ident.get("first"), ident.get("last")] if x)
    un = f"@{ident['username']}" if ident.get("username") else "—"
    return f"{name or '—'} ({un}), id <code>{ident.get('chat_id')}</code>"


def users_list(db: DB, crypto) -> str:
    rows = db.all_users()  # все: активные + бан
    if not rows:
        return "Пользователей пока нет."
    lines = []
    for i, r in enumerate(rows, 1):
        ident = crypto.dec(r["ident_enc"]) or {}
        mark = {"active": "✅", "banned": "⛔"}.get(r["status"], "·")
        tmark = "🎯" if r["tracking_enc"] else ""
        wl = " ⏳" if r["waitlist"] else ""
        lines.append(f"{i}. {mark}{tmark}{wl} {_ident_line(ident)} — "
                     f"с {r['joined']}")
    return "Список пользователей (номер -> /user N):\n" + "\n".join(lines)


def tracking_multiline(t: dict) -> str:
    lines = [
        f"Маршрут: {t.get('origin_name')} → {t.get('dest_name')}"
        f"{' → ' + t.get('origin_name') if not t.get('oneway') else ''}",
        f"Тип: {'только туда' if t.get('oneway') else 'туда и обратно'}",
        f"Полёт «туда»: {_win(t['out'])}",
    ]
    if not t.get("oneway") and t.get("ret"):
        lines.append(f"Полёт «обратно»: {_win(t['ret'])}")
    lines.append("⏰ Даты и время — местные, аэропортов вылета и "
                 "возврата (как на билете)" if not t.get("oneway")
                 else "⏰ Даты и время — местные, аэропорта вылета "
                      "(как на билете)")
    lines.append(f"Дата отсечки: {_fmt(t.get('cutoff'))}")
    lines.append(f"Целевая цена: {t.get('target'):,} ₽".replace(",", " "))
    lines.append(f"Дайджест: каждые {t.get('digest_every')} дн. в "
                 f"{t.get('digest_time')} МСК")
    c = t.get("calib")
    if c:
        lines.append(f"Калибровка: минимум {c.get('min')} ₽, "
                     f"типичный {c.get('p50')} ₽")
    return "\n".join(lines)


def user_card(db: DB, crypto, uid: str) -> str:
    row = db.get_user(uid)
    if not row:
        return "Пользователь не найден."
    ident = crypto.dec(row["ident_enc"]) or {}
    out = [f"👤 {_ident_line(ident)}",
           f"Статус: {row['status']}, с {row['joined']}"
           + (", в листе ожидания" if row["waitlist"] else "")]
    if row["tracking_enc"]:
        t = crypto.dec(row["tracking_enc"]) or {}
        out.append("Отслеживание:\n" + tracking_multiline(t))
        out.append(f"Последний опрос: {row['last_check'] or '—'}; "
                   f"дайджест: {row['last_digest'] or '—'}")
    else:
        out.append("Отслеживания нет.")
    return "\n".join(out)


def _win(w: dict) -> str:
    d0 = date.fromisoformat(w["from"])
    d1 = d0 + timedelta(days=int(w["days"]) - 1)
    if w["days"] == 1:
        return (f"{d0:%d.%m.%Y} {_DOW[d0.weekday()]} "
                f"{w['t1']}–{w['t2']}")
    return (f"{d0:%d.%m.%Y} {_DOW[d0.weekday()]} ({w['t1']}) – "
            f"{d1:%d.%m.%Y} {_DOW[d1.weekday()]} ({w['t2']})")


_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _fmt(iso: str | None) -> str:
    """«16.07.2027 Пт» — дата + день недели (или «—»)."""
    if not iso:
        return "—"
    d = date.fromisoformat(iso)
    return f"{d:%d.%m.%Y} {_DOW[d.weekday()]}"
