"""Telegram-бот: приём команд через getUpdates (без сервера 24/7).

Workflow каждые 30 минут в начале прогона забирает неразобранные апдейты
(Telegram хранит их 24 ч), обрабатывает и фиксирует offset в prices.db.
Задержка реакции на команду — до 30 минут, для задачи мониторинга это
нормально; отдельный сервер не нужен.

Тихий режим бана: сообщения забаненного пользователя игнорируются без
ответа и без изменения состояния.
"""
import json
import logging

import requests

from . import dialog, places, users as U

log = logging.getLogger(__name__)


def _tg(token: str, method: str, **params) -> dict:
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                      json=params, timeout=20)
    data = r.json() if r.headers.get("content-type", "").startswith(
        "application/json") else {}
    if not data.get("ok"):
        log.warning("TG %s error: %s %s", method, r.status_code, r.text[:150])
    return data


def send(token: str, chat_id, text: str) -> bool:
    try:
        return bool(_tg(token, "sendMessage", chat_id=chat_id, text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True).get("ok"))
    except requests.RequestException as e:
        log.warning("TG недоступен: %s", e)
        return False


HELP = (
    "✈️ <b>Бот слежения за ценами билетов</b>\n\n"
    "Команды:\n"
    "/start — регистрация и меню\n"
    "/watch — настроить отслеживание (9 шагов, значения по умолчанию — 0)\n"
    "/status — сводка текущего отслеживания\n"
    "/stop — удалить отслеживание (с подтверждением)\n"
    "/retry — активировать сохранённые параметры, если появилось место\n"
    "/cancel — прервать настройку\n"
    "/help — эта справка\n\n"
    "У одного пользователя одно активное отслеживание. Как только цена "
    "станет ниже вашей цели ИЛИ попадёт в минимум за 30 дней — придёт "
    "мгновенный пуш. Дайджест — по вашему расписанию."
)

ADMIN_HELP = (
    "🛠 <b>Админ-панель</b>\n"
    "/stats — сводка (пользователи, трекинги, лист ожидания)\n"
    "/users — список пользователей (номера для команд ниже)\n"
    "/user N — карточка пользователя N\n"
    "/del N — мягкое удаление (можно /start заново)\n"
    "/ban N — бан (тихий режим, без ответов)\n"
    "/unban N — снять бан"
)


def poll_and_process(token: str, db, crypto, cfg, admin_chat) -> int:
    """Забирает и обрабатывает апдейты. Возвращает число обработанных."""
    offset = int(db.get_meta("tg_offset", "0") or 0)
    try:
        data = _tg(token, "getUpdates", offset=offset, timeout=0,
                   allowed_updates=["message"])
    except requests.RequestException as e:
        log.warning("getUpdates недоступен: %s", e)
        return 0
    updates = (data.get("result") or [])[:30]  # ограничить батч
    for upd in updates:
        try:
            _process_update(token, db, crypto, cfg, admin_chat, upd)
        except Exception:
            log.exception("Ошибка обработки апдейта %s", upd.get("update_id"))
    if updates:
        db.set_meta("tg_offset", max(u["update_id"] for u in updates) + 1)
    return len(updates)


def _process_update(token, db, crypto, cfg, admin_chat, upd):
    msg = upd.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = (msg.get("text") or "").strip()
    from_user = msg.get("from") or {}
    if not chat_id or not text:
        return
    is_admin = str(chat_id) == str(admin_chat)

    uid, row, created = U.ensure_user(db, crypto, chat_id, from_user)
    if row["status"] == "banned":
        return  # тихий режим
    if created and not is_admin:
        send(token, chat_id,
             "👋 Добро пожаловать! Я слежу за ценами билетов 24/7. "
             + HELP)

    mu = cfg.get("multiuser") or {}

    # --- админ-команды ---
    if is_admin and _admin_command(token, db, crypto, uid, cfg, text):
        return

    # --- команды пользователя ---
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/start":
        _cmd_start(token, db, crypto, uid, row)
    elif cmd == "/help":
        send(token, chat_id, HELP)
    elif cmd == "/watch":
        _cmd_watch(token, db, crypto, uid, row, cfg, admin_chat, mu)
    elif cmd == "/retry":
        res = dialog.try_retry(db, crypto, uid, admin_chat, mu)
        if res == "ok":
            send(token, chat_id, "✅ Отслеживание активировано из "
                                 "сохранённых параметров! /status")
        elif res == "waitlist":
            send(token, chat_id, "⏳ Места всё ещё нет — как только "
                                 "освободится, сообщу.")
        else:
            send(token, chat_id, "Сохранённых параметров нет. "
                                 "Настройте отслеживание: /watch")
    elif cmd == "/status":
        t = crypto.dec(row["tracking_enc"]) if row["tracking_enc"] else None
        if t:
            send(token, chat_id, "📋 <b>Ваше отслеживание</b>\n"
                 + U.tracking_multiline(t)
                 + "\n\nУдалить: /stop; настроить заново: /stop → /watch")
        else:
            send(token, chat_id, "Отслеживания нет. Настроить: /watch")
    elif cmd == "/stop":
        if row["tracking_enc"]:
            send(token, chat_id, dialog.request_stop(db, crypto, uid))
        else:
            send(token, chat_id, "Удалять нечего — отслеживания нет.")
    elif cmd == "/cancel":
        db.update_user(uid, dialog_enc=None)
        send(token, chat_id, "Настройка прервана. /watch — начать заново.")
    else:
        # обычный текст — только если идёт диалог
        if row["dialog_enc"]:
            replies = dialog.feed(db, crypto, uid, text)
            for rep in replies:
                if isinstance(rep, tuple):  # ("SAVE", draft) от confirm
                    draft = rep[0]
                    res = dialog.finish_save(db, crypto, uid, admin_chat,
                                             draft, mu)
                    if res == "ok":
                        send(token, chat_id,
                             "✅ <b>Отслеживание активно!</b> Соберу "
                             "статистику маршрута и буду присылать дайджест. "
                             "/status — параметры, /stop — удаление.")
                    else:
                        dialog.save_waitlist_draft(db, crypto, uid, draft)
                        send(token, chat_id,
                             "⏳ Свободных мест для активных отслеживаний "
                             "нет (лимит бота). Вы в листе ожидания — "
                             "параметры сохранены, пришлю сообщение, когда "
                             "место освободится (или /retry).")
                else:
                    send(token, chat_id, rep)
        else:
            send(token, chat_id, "Не понял. Команды: /help. "
                                 "Настроить отслеживание: /watch")


def _cmd_start(token, db, crypto, uid, row):
    t = crypto.dec(row["tracking_enc"]) if row["tracking_enc"] else None
    if t:
        send(token, row_ident_chat(row, crypto) or "",
             "📋 <b>Ваше отслеживание</b>\n" + U.tracking_multiline(t)
             + "\n\nНастроить заново: /stop → /watch. Справка: /help")
        return
    st = crypto.dec(row["dialog_enc"]) if row["dialog_enc"] else {}
    if (st or {}).get("st") == "wl":
        send(token, row_ident_chat(row, crypto),
             "⏳ Вы в листе ожидания, параметры сохранены. "
             "Как только место освободится — пришлю сообщение "
             "(или попробуйте /retry).")
        return
    send(token, row_ident_chat(row, crypto), dialog.begin(db, crypto, uid))


def _cmd_watch(token, db, crypto, uid, row, cfg, admin_chat, mu):
    if row["tracking_enc"]:
        send(token, row_ident_chat(row, crypto),
             "У вас уже есть активное отслеживание (одно на пользователя). "
             "Чтобы настроить другое: /stop, затем /watch.")
        return
    send(token, row_ident_chat(row, crypto), dialog.begin(db, crypto, uid))


def row_ident_chat(row, crypto):
    ident = crypto.dec(row["ident_enc"]) or {}
    return str(ident.get("chat_id") or "")


def _admin_command(token, db, crypto, admin_uid_str, cfg, text) -> bool:
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    mu = cfg.get("multiuser") or {}
    if cmd == "/admin":
        send(token, row_admin_chat(crypto, admin_uid_str, db), ADMIN_HELP)
    elif cmd == "/stats":
        send(token, row_admin_chat(crypto, admin_uid_str, db),
             U.stats(db, mu))
    elif cmd == "/users":
        listing = U.users_list(db, crypto)
        uids = [r["uid"] for r in db.all_users()]
        db.set_meta("admin_list", json.dumps(uids))
        send(token, row_admin_chat(crypto, admin_uid_str, db), listing)
    elif cmd in ("/user", "/del", "/ban", "/unban"):
        chat = row_admin_chat(crypto, admin_uid_str, db)
        if len(parts) < 2 or not parts[1].isdigit():
            send(token, chat, f"Использование: {cmd} N (номер из /users)")
            return True
        uids = json.loads(db.get_meta("admin_list", "[]") or "[]")
        n = int(parts[1]) - 1
        if not (0 <= n < len(uids)):
            send(token, chat, "Нет такого номера — сначала /users")
            return True
        uid = uids[n]
        if cmd == "/user":
            send(token, chat, U.user_card(db, crypto, uid))
        elif cmd == "/del":
            U.admin_delete_user(db, crypto, uid)
            send(token, chat, "🗑 Пользователь удалён (сможет вернуться "
                              "через /start).")
        elif cmd == "/ban":
            U.admin_ban(db, crypto, uid, True)
            send(token, chat, "⛔ Пользователь забанен (тихий режим).")
        elif cmd == "/unban":
            U.admin_ban(db, crypto, uid, False)
            send(token, chat, "✅ Бан снят.")
    else:
        return False
    return True


def row_admin_chat(crypto, admin_uid_str, db):
    row = db.get_user(admin_uid_str)
    if row:
        return row_ident_chat(row, crypto)
    import os
    return os.environ.get("ADMIN_CHAT_ID") or os.environ.get("TG_CHAT_ID", "")
