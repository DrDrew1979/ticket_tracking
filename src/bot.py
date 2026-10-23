"""Telegram-бот: команды, текст и inline-кнопки (getUpdates, без сервера).

Два режима опроса:
1. live_loop — непрерывный long polling внутри прогона Actions
   (workflow tg-live-poll): ответ за 1–3 секунды, живёт ~5,5 ч.
2. poll_and_process — разовый короткий опрос (фолбэк monitor-прогона,
   по умолчанию выключен через SKIP_TG_POLL из-за конфликта 409).

Кнопки: все выборы визарда приходят как callback_query; сообщение
редактируется на месте (editMessageText / editMessageReplyMarkup),
всплывающие подсказки — answerCallbackQuery. При старте сессии бот
публикует меню команд (setMyCommands): обычным пользователям —
пользовательский набор, в чате администратора — плюс админ-команды.

Тихий режим бана: сообщения и кнопки забаненного игнорируются.
Offset обрабатанных апдейтов хранится в prices.db и коммитится после
каждой пачки — состояние переживает рестарты раннеров.
"""
import json
import logging
import time

import requests

from . import dialog, llm, places, users as U

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]
PRICE_WAIT_TEXT = ("⏳ Секундочку, вычисляю текущий уровень цен…")


def _tg(token: str, method: str, http_timeout: int = 20, **params) -> dict:
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                      json=params, timeout=http_timeout)
    data = r.json() if r.headers.get("content-type", "").startswith(
        "application/json") else {}
    if not data.get("ok"):
        log.warning("TG %s error: %s %s", method, r.status_code, r.text[:150])
    return data


def send(token: str, chat_id, text: str) -> bool:
    return send_kb(token, chat_id, text, None) is not None


def send_kb(token: str, chat_id, text: str, kb: dict | None) -> int | None:
    """Сообщение (с кнопками или без). Возвращает message_id или None."""
    try:
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True}
        if kb:
            params["reply_markup"] = json.dumps(kb)
        data = _tg(token, "sendMessage", **params)
        return (data.get("result") or {}).get("message_id")
    except requests.RequestException as e:
        log.warning("TG недоступен: %s", e)
        return None


def _edit(token: str, chat_id, msg_id, text: str, kb: dict | None) -> bool:
    try:
        params = {"chat_id": chat_id, "message_id": msg_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True}
        if kb:
            params["reply_markup"] = json.dumps(kb)
        data = _tg(token, "editMessageText", **params)
        if data.get("ok"):
            return True
        # повторное нажатие той же кнопки: контент не изменился — это успех
        return "message is not modified" in (data.get("description") or "")
    except requests.RequestException as e:
        log.warning("TG edit недоступен: %s", e)
        return False


def _edit_kb(token: str, chat_id, msg_id, kb: dict) -> bool:
    try:
        data = _tg(token, "editMessageReplyMarkup", chat_id=chat_id,
                   message_id=msg_id, reply_markup=json.dumps(kb))
        if data.get("ok"):
            return True
        return "message is not modified" in (data.get("description") or "")
    except requests.RequestException as e:
        log.warning("TG edit_kb недоступен: %s", e)
        return False


def _answer_cb(token: str, cb_id: str, toast: str | None = None):
    try:
        params = {"callback_query_id": cb_id}
        if toast:
            params["text"] = toast[:190]
            params["show_alert"] = False
        _tg(token, "answerCallbackQuery", **params)
    except requests.RequestException:
        pass


def _show(token, chat_id, msg_id, m: dict, edit: bool) -> None:
    """Показать сообщение: правкой существующего или новым."""
    if edit and msg_id and _edit(token, chat_id, msg_id, m["text"],
                                 m.get("kb")):
        return
    send_kb(token, chat_id, m["text"], m.get("kb"))


HELP = (
    "✈️ <b>Бот слежения за ценами билетов</b>\n\n"
    "Команды (и кнопка «Меню» в интерфейсе Telegram):\n"
    "/watch — настроить отслеживание (7 укрупнённых шагов с кнопками)\n"
    "/edit — изменить отслеживание по всем параметрам (прежние значения "
    "подставлены, любой блок можно оставить как есть)\n"
    "/status — сводка текущего отслеживания\n"
    "/stop — удалить отслеживание (с подтверждением)\n"
    "/retry — активировать сохранённые параметры, если появилось место\n"
    "/menu — меню команд кнопками\n"
    "/cancel — прервать настройку\n"
    "/help — эта справка\n\n"
    "У одного пользователя одно активное отслеживание. Как только цена "
    "станет ниже вашей цели ИЛИ попадёт в минимум за 30 дней — придёт "
    "мгновенный пуш. Дайджест — по вашему расписанию."
)

ADMIN_HELP = (
    "🛠 <b>Админ-панель</b>\n"
    "/stats — сводка (пользователи, трекинги, лист ожидания)\n"
    "/users — список пользователей с кнопками «инфо / удал / бан»\n"
    "/user N — карточка пользователя N\n"
    "/del N — мягкое удаление (можно /start заново)\n"
    "/ban N — бан (тихий режим, без ответов)\n"
    "/unban N — снять бан"
)

USER_COMMANDS = [
    {"command": "start", "description": "Регистрация и меню"},
    {"command": "watch", "description": "Настроить отслеживание"},
    {"command": "edit", "description": "Изменить отслеживание"},
    {"command": "status", "description": "Сводка отслеживания"},
    {"command": "stop", "description": "Удалить отслеживание"},
    {"command": "retry", "description": "Активировать сохранённое"},
    {"command": "menu", "description": "Меню команд"},
    {"command": "help", "description": "Справка"},
]
ADMIN_COMMANDS = [
    {"command": "stats", "description": "Статистика бота"},
    {"command": "users", "description": "Пользователи (панель)"},
    {"command": "admin", "description": "Админ-команды"},
]


def set_my_commands(token: str, admin_chat: str) -> None:
    """Публикует меню команд (кнопка «Меню» в интерфейсе Telegram)."""
    try:
        _tg(token, "setMyCommands", commands=USER_COMMANDS)
        if admin_chat:
            try:
                scope = {"type": "chat",
                         "chat_id": int(admin_chat)}
            except (TypeError, ValueError):
                scope = {"type": "chat", "chat_id": admin_chat}
            _tg(token, "setMyCommands", commands=USER_COMMANDS
                + ADMIN_COMMANDS, scope=scope)
    except requests.RequestException:
        pass


def menu_kb(is_admin: bool) -> dict:
    rows = [[{"text": "🧭 Настроить отслеживание",
              "callback_data": "d|cmd|watch"}],
            [{"text": "✏️ Изменить отслеживание",
              "callback_data": "d|cmd|edit"},
             {"text": "📋 Статус", "callback_data": "d|cmd|status"}],
            [{"text": "🚫 Удалить", "callback_data": "d|cmd|stop"},
             {"text": "❓ Помощь", "callback_data": "d|cmd|help"}]]
    if is_admin:
        rows.append([{"text": "📊 Статистика",
                      "callback_data": "d|cmd|stats"},
                     {"text": "👥 Пользователи",
                      "callback_data": "d|cmd|users"}])
    return {"inline_keyboard": rows}


def menu_text() -> str:
    return ("📋 <b>Меню</b> — выберите действие кнопкой.\nКоманды также "
            "доступны через кнопку «Меню» рядом с полем ввода.")


def poll_and_process(token: str, db, crypto, cfg, admin_chat) -> int:
    """Разовый короткий опрос (фолбэк monitor-прогона)."""
    set_my_commands(token, admin_chat)
    offset = int(db.get_meta("tg_offset", "0") or 0)
    try:
        data = _tg(token, "getUpdates", offset=offset, timeout=0,
                   allowed_updates=ALLOWED_UPDATES)
    except requests.RequestException as e:
        log.warning("getUpdates недоступен: %s", e)
        return 0
    updates = (data.get("result") or [])[:30]
    for upd in updates:
        try:
            _process_update(token, db, crypto, cfg, admin_chat, upd)
        except Exception:
            log.exception("Ошибка обработки апдейта %s", upd.get("update_id"))
    if updates:
        db.set_meta("tg_offset", max(u["update_id"] for u in updates) + 1)
    return len(updates)


def live_loop(token: str, db, crypto, cfg, admin_chat, deadline: float,
              on_batch=None) -> int:
    """Непрерывный long polling до дедлайна (точка time.monotonic())."""
    set_my_commands(token, admin_chat)
    offset = int(db.get_meta("tg_offset", "0") or 0)
    processed = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 3:
            break
        lp = 25 if remaining >= 35 else max(1, int(remaining) - 5)
        try:
            data = _tg(token, "getUpdates", offset=offset, timeout=lp,
                       http_timeout=lp + 15, allowed_updates=ALLOWED_UPDATES)
        except requests.RequestException as e:
            log.warning("getUpdates недоступен: %s", e)
            time.sleep(5)
            continue
        if not data.get("ok"):
            try:
                ra = int((data.get("parameters") or {}).get("retry_after") or 0)
            except (TypeError, ValueError):
                ra = 0
            time.sleep(min(max(ra, 3), 30))
            continue
        updates = (data.get("result") or [])[:30]
        for upd in updates:
            try:
                _process_update(token, db, crypto, cfg, admin_chat, upd)
            except Exception:
                log.exception("Ошибка обработки апдейта %s",
                              upd.get("update_id"))
        if updates:
            offset = max(u["update_id"] for u in updates) + 1
            db.set_meta("tg_offset", offset)
            processed += len(updates)
            if on_batch:
                try:
                    on_batch()
                except Exception:
                    log.exception("on_batch (коммит БД) не удался — "
                                  "продолжаем; Telegram вернёт апдейты")
    return processed


# ------------------------------------------------------------ маршрутизация

def _process_update(token, db, crypto, cfg, admin_chat, upd):
    cb = upd.get("callback_query")
    if cb:
        _process_callback(token, db, crypto, cfg, admin_chat, cb)
        return
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
        send_kb(token, chat_id,
                "👋 Добро пожаловать! Я слежу за ценами билетов 24/7.\n\n"
                + menu_text(), menu_kb(is_admin))
    mu = cfg.get("multiuser") or {}
    if is_admin and _admin_command(token, db, crypto, uid, cfg, text,
                                   admin_chat):
        return
    cmd = text.split()[0].split("@")[0].lower()
    if cmd == "/menu":
        send_kb(token, chat_id, menu_text(), menu_kb(is_admin))
    elif cmd == "/start":
        _cmd_start(token, db, crypto, uid, row, is_admin, created)
    elif cmd == "/help":
        send(token, chat_id, HELP + ("\n\n" + ADMIN_HELP if is_admin else ""))
    elif cmd in ("/watch", "/edit"):
        _cmd_watch(token, db, crypto, uid, row, cfg, admin_chat, mu,
                   edit=(cmd == "/edit"))
    elif cmd == "/retry":
        res = dialog.try_retry(db, crypto, uid, admin_chat, mu)
        if res == "ok":
            send_kb(token, chat_id, "✅ Отслеживание активировано из "
                                    "сохранённых параметров!", menu_kb(False))
        elif res == "waitlist":
            send(token, chat_id, "⏳ Места всё ещё нет — как только "
                                 "освободится, сообщу.")
        else:
            send(token, chat_id, "Сохранённых параметров нет. "
                                 "Настройте отслеживание: /watch")
    elif cmd == "/status":
        t = crypto.dec(row["tracking_enc"]) if row["tracking_enc"] else None
        if t:
            send_kb(token, chat_id,
                    "📋 <b>Ваше отслеживание</b>\n"
                    + U.tracking_multiline(t)
                    + "\n\nИзменить: /edit; удалить: /stop",
                    menu_kb(False))
        else:
            send(token, chat_id, "Отслеживания нет. Настроить: /watch")
    elif cmd == "/stop":
        if row["tracking_enc"]:
            m = dialog.request_stop(db, crypto, uid)
            send_kb(token, chat_id, m["text"], m.get("kb"))
        else:
            send(token, chat_id, "Удалять нечего — отслеживания нет.")
    elif cmd == "/cancel":
        db.update_user(uid, dialog_enc=None)
        send(token, chat_id, "Настройка прервана. /watch — начать заново.")
    else:
        if row["dialog_enc"]:
            res = dialog.feed(db, crypto, uid, text)
            _handle_dialog_result(token, db, crypto, cfg, admin_chat, mu,
                                  chat_id, None, res)
        else:
            send(token, chat_id, "Не понял. Команды: /help. "
                                 "Настроить отслеживание: /watch")


def _process_callback(token, db, crypto, cfg, admin_chat, cb):
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    from_user = cb.get("from") or {}
    if not chat_id:
        _answer_cb(token, cb.get("id"))
        return
    is_admin = str(chat_id) == str(admin_chat)
    cb_id = cb.get("id")
    if data.startswith("d|adm|") and not is_admin:
        _answer_cb(token, cb_id, "Только для администратора")
        return
    uid, row, _ = U.ensure_user(db, crypto, chat_id, from_user)
    if row["status"] == "banned":
        _answer_cb(token, cb_id)
        return
    mu = cfg.get("multiuser") or {}
    if data.startswith("d|adm|"):
        _admin_cb(token, db, crypto, cfg, admin_chat, uid, chat_id, msg_id,
                  cb_id, data)
        return
    if data.startswith("d|cmd|"):
        _answer_cb(token, cb_id)
        _cmd_cb(token, db, crypto, uid, row, cfg, admin_chat, mu, chat_id,
                msg_id, data.split("|")[2], is_admin)
        return
    res = dialog.cb(db, crypto, uid, data)
    _handle_dialog_result(token, db, crypto, cfg, admin_chat, mu, chat_id,
                          (msg_id, cb_id), res)


# ------------------------------------------------------- результат диалога

def _handle_dialog_result(token, db, crypto, cfg, admin_chat, mu, chat_id,
                          tapped, res):
    """tapped: (msg_id, cb_id) для правки сообщения/тостов, None для текста."""
    msg_id = tapped[0] if tapped else None
    cb_id = tapped[1] if tapped else None
    if isinstance(res, tuple) and len(res) == 2 and res[1] == "SAVE":
        draft = res[0]
        _finish_tracking(token, db, crypto, cfg, admin_chat, mu, chat_id,
                         msg_id, draft)
        return
    if not isinstance(res, dict):
        for m in (res or []):
            send_kb(token, chat_id, m["text"], m.get("kb"))
        return
    if cb_id is not None and res.get("toast"):
        _answer_cb(token, cb_id, res.get("toast"))
    elif cb_id is not None:
        _answer_cb(token, cb_id)
    if res.get("fetch"):
        echo = res.get("echo")
        wait = PRICE_WAIT_TEXT + (f"\n\n{echo}" if echo else "")
        if msg_id:
            _edit(token, chat_id, msg_id, wait, None)
        else:
            send(token, chat_id, wait)
        minp = _fetch_min_price(cfg, res.get("draft") or {})
        m = dialog.price_ready(db, crypto, crypto.uid(chat_id), minp)
        _show(token, chat_id, msg_id, m, edit=True)
        return
    if res.get("kb_only") and msg_id:
        _edit_kb(token, chat_id, msg_id, res["kb_only"])
        if res.get("msgs"):
            for m in res["msgs"]:
                send_kb(token, chat_id, m["text"], m.get("kb"))
        return
    msgs = res.get("msgs") or []
    edit_first = bool(res.get("edit")) and msg_id is not None
    for i, m in enumerate(msgs):
        if i == 0 and edit_first:
            if not _edit(token, chat_id, msg_id, m["text"], m.get("kb")):
                send_kb(token, chat_id, m["text"], m.get("kb"))
        else:
            send_kb(token, chat_id, m["text"], m.get("kb"))


def _finish_tracking(token, db, crypto, cfg, admin_chat, mu, chat_id,
                     msg_id, draft):
    res = dialog.finish_save(db, crypto, crypto.uid(chat_id), admin_chat,
                             draft, mu)
    if res == "ok":
        try:
            joke = llm.make(draft.get("origin_name"),
                            draft.get("dest_name"),
                            int((draft.get("out") or {})
                                .get("from", "")[5:7] or 1))
        except (ValueError, IndexError):
            joke = ""   # месяц не распознался — подтверждение без шутки
        text = ("✅ <b>Отслеживание активно!</b> Соберу статистику "
                "маршрута и буду присылать дайджест. /status — параметры, "
                "/edit — изменение, /stop — удаление.")
        if joke:
            text += f"\n\n🎭 <i>{joke}</i>"
        _edit(token, chat_id, msg_id, text, menu_kb(False)) \
            if msg_id else send_kb(token, chat_id, text, menu_kb(False))
    else:
        dialog.save_waitlist_draft(db, crypto, crypto.uid(chat_id), draft)
        send(token, chat_id,
             "⏳ Свободных мест для активных отслеживаний нет (лимит "
             "бота). Вы в листе ожидания — параметры сохранены, пришлю "
             "сообщение, когда место освободится (или /retry).")


def _fetch_min_price(cfg: dict, draft: dict) -> int | None:
    """ОДИН запрос к кэшу Aviasales по первой паре дат; минимум цены."""
    import os
    token = os.environ.get("TP_TOKEN")
    out = draft.get("out") or {}
    if not token or not out.get("from"):
        return None
    try:
        from .fetcher import TPClient
        client = TPClient(token)
        ret = None
        if not draft.get("oneway"):
            ret = (draft.get("ret") or {}).get("from")
        offers = client.fetch_roundtrip(draft["origin"], draft["dest"],
                                        out["from"], ret,
                                        cfg.get("currency", "rub"))
        prices = [int(o["price"]) for o in offers if o.get("price")]
        return min(prices) if prices else None
    except Exception as e:  # noqa: BLE001
        log.warning("Поиск цены для цели не удался: %s", e)
        return None


# ------------------------------------------------------------ команды

def _cmd_start(token, db, crypto, uid, row, is_admin=False, created=False):
    chat = row_ident_chat(row, crypto)
    t = crypto.dec(row["tracking_enc"]) if row["tracking_enc"] else None
    if t:
        _show_or_send(token, chat,
                      _msg_dict("📋 <b>Ваше отслеживание</b>\n"
                                + U.tracking_multiline(t)
                                + "\n\nИзменить: /edit; удалить: /stop",
                                menu_kb(is_admin)))
        return
    st = crypto.dec(row["dialog_enc"]) if row["dialog_enc"] else {}
    if (st or {}).get("st") == "wl":
        send(token, chat, "⏳ Вы в листе ожидания, параметры сохранены. "
                          "Как только место освободится — пришлю "
                          "сообщение (или попробуйте /retry).")
        return
    if created:
        # новый пользователь: сразу запуск мастера настройки
        m = dialog.begin(db, crypto, uid)
        send_kb(token, chat, m["text"], m.get("kb"))
        return
    # существующий пользователь без трекинга — меню
    send_kb(token, chat, menu_text(), menu_kb(is_admin))


def _msg_dict(text, kb=None):
    return {"text": text, "kb": kb}


def _show_or_send(token, chat, m, msg_id=None):
    if msg_id and _edit(token, chat, msg_id, m["text"], m.get("kb")):
        return
    send_kb(token, chat, m["text"], m.get("kb"))


def _cmd_watch(token, db, crypto, uid, row, cfg, admin_chat, mu,
               edit=False):
    chat = row_ident_chat(row, crypto)
    if edit:
        t = crypto.dec(row["tracking_enc"]) if row["tracking_enc"] else None
        if t:
            m = dialog.begin_edit(db, crypto, uid, t)
        else:
            m = dialog.begin(db, crypto, uid)
        send_kb(token, chat, m["text"], m.get("kb"))
        return
    if row["tracking_enc"]:
        send(token, chat,
             "У вас уже есть активное отслеживание (одно на пользователя). "
             "Изменить его: /edit — прежние значения подставлю, любой "
             "блок можно оставить кнопкой «⏭ Оставить».")
        return
    m = dialog.begin(db, crypto, uid)
    send_kb(token, chat, m["text"], m.get("kb"))


def _cmd_cb(token, db, crypto, uid, row, cfg, admin_chat, mu, chat_id,
            msg_id, cmd, is_admin):
    """Кнопки меню: те же действия, что и команды."""
    if cmd == "watch":
        if row["tracking_enc"]:
            _show_or_send(token, chat_id, _msg_dict(
                "У вас уже есть активное отслеживание. Изменить: /edit "
                "(кнопка ниже).", {"inline_keyboard": [[
                    {"text": "✏️ Изменить отслеживание",
                     "callback_data": "d|cmd|edit"}]]}), msg_id)
            return
        m = dialog.begin(db, crypto, uid)
        _show_or_send(token, chat_id, m, msg_id)
    elif cmd == "edit":
        _cmd_watch(token, db, crypto, uid, row, cfg, admin_chat, mu,
                   edit=True)
        _edit(token, chat_id, msg_id,
              "Открываю изменение отслеживания — смотрите сообщение "
              "выше.", None) if msg_id else None
    elif cmd == "status":
        t = crypto.dec(row["tracking_enc"]) if row["tracking_enc"] else None
        if t:
            _show_or_send(token, chat_id, _msg_dict(
                "📋 <b>Ваше отслеживание</b>\n" + U.tracking_multiline(t)
                + "\n\nИзменить: /edit; удалить: /stop",
                menu_kb(False)), msg_id)
        else:
            _show_or_send(token, chat_id, _msg_dict(
                "Отслеживания нет. Настроить: /watch",
                {"inline_keyboard": [[
                    {"text": "🧭 Настроить отслеживание",
                     "callback_data": "d|cmd|watch"}]]}), msg_id)
    elif cmd == "stop":
        if row["tracking_enc"]:
            m = dialog.request_stop(db, crypto, uid)
            _show_or_send(token, chat_id, m, msg_id)
        else:
            _answer_toastless(token, chat_id, msg_id,
                              "Удалять нечего — отслеживания нет.")
    elif cmd == "help":
        _show_or_send(token, chat_id, _msg_dict(
            HELP + ("\n\n" + ADMIN_HELP if is_admin else "")), msg_id)
    elif cmd == "menu":
        _show_or_send(token, chat_id, _msg_dict(menu_text(),
                                                menu_kb(is_admin)), msg_id)
    elif cmd == "stats" and is_admin:
        send(token, chat_id, U.stats(db, mu))
    elif cmd == "users" and is_admin:
        _admin_users_panel(token, db, crypto, chat_id, msg_id)


def _answer_toastless(token, chat_id, msg_id, text):
    if msg_id:
        _edit(token, chat_id, msg_id, text, None)
    else:
        send(token, chat_id, text)


def row_ident_chat(row, crypto):
    ident = crypto.dec(row["ident_enc"]) or {}
    return str(ident.get("chat_id") or "")


# ------------------------------------------------------- админ-панель

def _admin_users_panel(token, db, crypto, chat_id, msg_id=None):
    """Список пользователей + кнопки «инфо / удал / бан(разбан)» в строке."""
    rows_all = db.all_users()
    if not rows_all:
        text = "Пользователей пока нет."
        kb = None
    else:
        kb_rows = []
        for i, r in enumerate(rows_all, 1):
            ident = crypto.dec(r["ident_enc"]) or {}
            name = (ident.get("first") or ident.get("username")
                    or ident.get("chat_id") or "?")
            uid = r["uid"]
            banned = r["status"] == "banned"
            mark = "⛔ " if banned else ""
            tmark = "🎯" if r["tracking_enc"] else ""
            wl = " ⏳" if r["waitlist"] else ""
            kb_rows.append([
                {"text": f"ℹ️ {i}. {mark}{tmark}{wl}{name}"[:60],
                 "callback_data": f"d|adm|info|{uid}"},
                {"text": "🗑", "callback_data": f"d|adm|del|{uid}"},
                {"text": "✅ разбан" if banned else "🚫 бан",
                 "callback_data": (f"d|adm|unban|{uid}" if banned
                                   else f"d|adm|ban|{uid}"),
                 }])
        text = (f"👥 Пользователей: {len(rows_all)}.\n"
                "Нажмите имя — карточка; 🗑 — удалить (с подтверждением); "
                "🚫/✅ — бан/разбан. Пометки: 🎯 трекинг, ⏳ лист "
                "ожидания, ⛔ бан.")
        kb = {"inline_keyboard": kb_rows}
    if msg_id:
        _edit(token, chat_id, msg_id, text, kb)
    else:
        send_kb(token, chat_id, text, kb)


def _admin_cb(token, db, crypto, cfg, admin_chat, admin_uid, chat_id,
              msg_id, cb_id, data):
    parts = data.split("|")  # d|adm|<action>|<uid>
    action = parts[2]
    target_uid = parts[3] if len(parts) > 3 else ""
    if target_uid == admin_uid and action in ("del", "del2", "ban"):
        _answer_cb(token, cb_id, "Над администратором эти действия "
                                 "недоступны")
        return
    if action == "list":
        _answer_cb(token, cb_id)
        _admin_users_panel(token, db, crypto, chat_id, msg_id)
        return
    if action == "info":
        _answer_cb(token, cb_id)
        card = U.user_card(db, crypto, target_uid)
        row = db.get_user(target_uid)
        banned = bool(row) and row["status"] == "banned"
        kb = {"inline_keyboard": [[
            {"text": "⬅️ К списку", "callback_data": "d|adm|list"},
            {"text": "🗑", "callback_data": f"d|adm|del|{target_uid}"},
            {"text": "✅ разбан" if banned else "🚫 бан",
             "callback_data": (f"d|adm|unban|{target_uid}" if banned
                               else f"d|adm|ban|{target_uid}")}]]}
        _edit(token, chat_id, msg_id, card, kb)
        return
    if action == "del":
        _answer_cb(token, cb_id)
        row = db.get_user(target_uid)
        name = "?"
        if row:
            ident = crypto.dec(row["ident_enc"]) or {}
            name = (ident.get("first") or ident.get("username")
                    or ident.get("chat_id") or "?")
        kb = {"inline_keyboard": [[
            {"text": "🗑 Да, удалить",
             "callback_data": f"d|adm|del2|{target_uid}"},
            {"text": "⬅️ Отмена", "callback_data": "d|adm|list"}]]}
        _edit(token, chat_id, msg_id,
              f"Удалить пользователя <b>{name}</b>? Запись будет "
              "стёрта (можно вернуться через /start).", kb)
        return
    if action == "del2":
        U.admin_delete_user(db, crypto, target_uid)
        _answer_cb(token, cb_id, "Пользователь удалён")
        _admin_users_panel(token, db, crypto, chat_id, msg_id)
        return
    if action == "ban":
        U.admin_ban(db, crypto, target_uid, True)
        _answer_cb(token, cb_id, "Забанен (тихий режим)")
        _admin_users_panel(token, db, crypto, chat_id, msg_id)
        return
    if action == "unban":
        U.admin_ban(db, crypto, target_uid, False)
        _answer_cb(token, cb_id, "Бан снят")
        _admin_users_panel(token, db, crypto, chat_id, msg_id)
        return
    _answer_cb(token, cb_id)


def _admin_command(token, db, crypto, admin_uid_str, cfg, text,
                   admin_chat) -> bool:
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    mu = cfg.get("multiuser") or {}
    chat = row_admin_chat(crypto, admin_uid_str, db)
    if cmd == "/admin":
        send(token, chat, ADMIN_HELP)
    elif cmd == "/stats":
        send(token, chat, U.stats(db, mu))
    elif cmd == "/users":
        _admin_users_panel(token, db, crypto, chat)
    elif cmd in ("/user", "/del", "/ban", "/unban"):
        if len(parts) < 2 or not parts[1].isdigit():
            send(token, chat, f"Использование: {cmd} N (номер из /users)")
            return True
        rows_all = db.all_users()
        n = int(parts[1]) - 1
        if not (0 <= n < len(rows_all)):
            send(token, chat, "Нет такого номера — сначала /users")
            return True
        uid = rows_all[n]["uid"]
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
