"""Диалог настройки отслеживания — версия 2026-10 «кнопки».

Укрупнённые шаги (вместо 15 мелких):
  1. Город/аэропорт вылета (ввод названия -> выбор из кнопок)
  2. Город/аэропорт прилёта
  3. Тип поездки (кнопки «только туда» / «туда и обратно»)
  4. Полёт «туда»: дата (календарь, даты за 400 дней помечены ✖) +
     гибкая настройка периода (кнопки-диапазоны) + время (по умолчанию
     весь день 00:00–23:59, опционально «Настроить время суток»)
  5. Полёт «обратно» (только для «туда и обратно», аналогично шагу 4)
  6. Дата отсечки (по умолчанию: за 14 дней до даты вылета, но
     не раньше сегодняшнего дня; календарь) + целевая цена (один поиск
     в кэше Aviasales, кнопка с ценой −15%, свой ввод или «без цели»)
  7. Дайджест: частота (кнопки, по умолчанию 1 день) и время (часы
     [ЧЧ]:[ММ], по умолчанию 09:00 МСК), кнопка «Подтвердить»

Финальное меню — сводка с кнопками «Подтвердить» и «Изменить» («Изменить»
открывает меню блоков, правка любого блока с возвратом к проверке).

Все выборы — inline-кнопки (editMessageText — одно сообщение редактируется
на месте). Текстовый ввод остался там, где без него нельзя: названия
городов, своя цена, время «1057»/«10:57» как дублирующий способ.

Режим изменения: /edit открывает СВОДКУ текущих параметров («Изменить»
-> меню блоков -> правка -> возврат к сводке) — весь визард заново не
проходится. /watch после /stop подставляет прежние значения в каждый
шаг; у каждого блока есть кнопка «⏭ Оставить …». Состояние шифруется
(dialog_enc в prices.db) — переживает рестарты раннеров.
"""
import logging
from datetime import date, timedelta

from . import holidays  # noqa: F401  (нужен ui.py; импорт для связности)
from . import places, ui

log = logging.getLogger(__name__)

_MAX_HORIZON_DAYS = 400

_CLEAN_FIELDS = ("origin", "origin_name", "dest", "dest_name", "oneway",
                 "out", "ret", "cutoff", "target", "digest_every",
                 "digest_time")


def _msg(text: str, kb: dict | None = None) -> dict:
    return {"text": text, "kb": kb}


def _toast(s: str) -> str:
    return s


# ---------------------------------------------------------------- утилиты

def _load(crypto, row) -> dict:
    return crypto.dec(row["dialog_enc"]) or {}


def _save(db, crypto, uid, state):
    db.update_user(uid, dialog_enc=crypto.enc(state) if state else None)


def _cur(st, key):
    return (st.get("draft") or {}).get(key)


def _d(iso) -> str:
    return f"{date.fromisoformat(iso):%d.%m.%Y}"


def _dw(d) -> str:
    """Двухбуквенный день недели: Пн/Вт/Ср/Чт/Пт/Сб/Вс."""
    return ui.DOW[d.weekday()]


def _ddw(d) -> str:
    """«16.07.2027 Пт» — дата + двухбуквенный день недели."""
    if not isinstance(d, date):
        d = date.fromisoformat(d)
    return f"{d:%d.%m.%Y} {_dw(d)}"


def _rub(n) -> str:
    return f"{n:,} ₽".replace(",", " ")


def _days_word(n: int) -> str:
    if n == 1:
        return "1 день"
    if 2 <= n <= 4:
        return f"{n} дня"
    return f"{n} дней"


def _every_word(n: int) -> str:
    return "ежедневно" if n == 1 else f"каждые {n} " + ("дня" if n < 5 else "дней")


def _trip_word(oneway) -> str:
    return "только туда" if oneway else "туда и обратно"


def _target_word(v) -> str:
    return _rub(v) if v else "не задана (уведомления по цене выключены)"


def _win_dates(w) -> tuple:
    d0 = date.fromisoformat(w["from"])
    return d0, d0 + timedelta(days=int(w["days"]) - 1)


def _win_range(w) -> str:
    """«16.07.2027 Пт – 18.07.2027 Вс» или одна дата с днём недели."""
    d0, d1 = _win_dates(w)
    return _ddw(d0) if d0 == d1 else f"{_ddw(d0)} – {_ddw(d1)}"


def _ok(what: str, kept: bool = False) -> str:
    return f"✅ Принято: {what}" + (" (оставлено прежнее)" if kept else "")


# ------------------------------------------------------------ шаги/шапка

_BLK_N = {"city_from": 1, "city_pick": 1, "city_to": 2, "trip": 3,
          "out_date": 4, "out_days": 4, "out_time_ask": 4,
          "out_clk1": 4, "out_clk2": 4,
          "ret_date": 5, "ret_days": 5, "ret_time_ask": 5,
          "ret_clk1": 5, "ret_clk2": 5,
          "cutoff": 6, "target": 6, "target_manual": 6, "digest": 7,
          "dig_clk": 7}


def _head(st, title: str) -> str:
    n = _BLK_N.get(st["st"], 1)
    total = 6 if (_cur(st, "oneway")) else 7
    if _cur(st, "oneway") and n > 4:
        n -= 1
    return f"🧭 <b>Шаг {n}/{total}. {title}</b>\n\n"


# ---------------------------------------------------------------- парсеры

def parse_date(s: str) -> date | None:
    """DD.MM.YYYY | DD.MM.YY | DD.MM. None — не удалось разобрать."""
    s = (s or "").strip().replace("-", ".")
    parts = s.split(".")
    if not (2 <= len(parts) <= 3) or not all(p.isdigit() for p in parts):
        return None
    d, m = int(parts[0]), int(parts[1])
    today = date.today()
    if len(parts) == 2:
        try:
            dt = date(today.year, m, d)
        except ValueError:
            return None
        if dt < today:
            try:
                dt = date(today.year + 1, m, d)
            except ValueError:
                return None
        return dt
    y = int(parts[2])
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_time(s: str) -> str | None:
    """ЧЧ:ММ (00–23 / 00–59) -> 'ЧЧ:ММ'. None — ошибка."""
    s = (s or "").strip()
    parts = s.split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) <= 2
                                  for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def parse_time_text(s: str) -> str | None:
    """«1057» или «10:57» -> «10:57». None — ошибка (часы 00–23)."""
    digits = (s or "").strip().replace(":", "").replace(".", "")
    digits = digits.replace("-", "").replace(" ", "")
    if digits.isdigit() and len(digits) == 4:
        h, m = int(digits[:2]), int(digits[2:])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
        return None
    return parse_time(s)


# ------------------------------------------------------------- календарь

def _cal_default_month(st, wkey: str) -> tuple[int, int]:
    d = (_cur(st, wkey) or {}).get("from")
    if not d:
        d = (_cur(st, "out") or {}).get("from")
    base = date.fromisoformat(d) if d else date.today()
    return base.year, base.month


def _cal_selected(st, scope: str) -> str | None:
    if scope == "cut":
        return _cur(st, "cutoff")
    return (_cur(st, scope) or {}).get("from")


def _cal_bounds(scope: str, st) -> tuple:
    today = date.today()
    horizon = today + timedelta(days=_MAX_HORIZON_DAYS)
    if scope == "out":
        return today, horizon
    if scope == "ret":
        out = _cur(st, "out") or {}
        lo = today
        if out.get("from") and out.get("days"):
            lo = max(lo, _win_dates(out)[1])
        return lo, horizon
    # cut: от сегодня до начала окна вылета
    out = _cur(st, "out") or {}
    hi = date.fromisoformat(out["from"]) if out.get("from") else horizon
    return today, min(hi, horizon)


# ---------------------------------------------------------------- запуск

def _draft_from_prev(prev: dict) -> dict:
    d = {k: prev[k] for k in _CLEAN_FIELDS
         if k in prev and prev[k] is not None}
    if d.get("oneway"):
        d.pop("ret", None)
    return d


def _new_state(edit: bool, last: dict | None) -> dict:
    st = {"st": "city_from", "draft": {}, "ui": {}}
    if edit and last:
        st["edit"] = True
        st["last"] = last
        st["draft"] = _draft_from_prev(last)
    return st


def _greeting(st) -> dict:
    note = ""
    if st.get("edit"):
        note = ("Прежние значения подставлены — каждый блок можно "
                "оставить кнопкой «⏭ Оставить».\n\n")
    kb = None
    if st.get("edit") and _cur(st, "origin_name"):
        kb = ui_skip("cf", f"Оставить «{_short_name(_cur(st, 'origin_name'))}»")
    text = ("🧭 <b>Настройка отслеживания</b> — 7 коротких шагов\n\n"
            + note + _head(st, "Город вылета")
            + "Введите город (или аэропорт) вылета — например «Москва» "
              "или «Шереметьево». Опечатки не страшны; если городов с "
              "таким названием несколько — добавьте страну через "
              "запятую: «Мельбурн, Австралия».\n"
            + "<i>Прервать: /cancel</i>")
    return _msg(text, kb)


def _short_name(label: str | None) -> str:
    """«Россия, Москва (MOW), все аэропорты» -> «Москва (MOW)»."""
    s = (label or "").split("(")[0].strip().rstrip(",")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 2 and parts[0].lower() in ("россия",):
        return parts[1]
    return parts[0] if parts else (label or "—")


def ui_skip(block: str, label: str) -> dict:
    return {"inline_keyboard": [[{"text": f"⏭ {label}",
                                  "callback_data": f"d|keep|{block}"}]]}


def begin(db, crypto, uid: str) -> dict:
    """Старт визарда: с нуля или с прежними значениями после /stop."""
    prev_st = _load(crypto, db.get_user(uid))
    if prev_st.get("st") == "wl":
        return _msg("⏳ Вы в листе ожидания — ваши параметры уже "
                    "сохранены. Когда появится место, пришлю сообщение "
                    "(или попробуйте /retry).")
    last = prev_st.get("last") if prev_st.get("st") == "stopped" else None
    st = _new_state(bool(last), last)
    _save(db, crypto, uid, st)
    return _greeting(st)


def begin_edit(db, crypto, uid: str, tracking: dict) -> dict:
    """/edit при активном трекинге: сразу сводка -> выбор блока -> правка.

    Весь визард заново НЕ запускается (раньше это выглядело как рестарт
    настройки с экрана 1). Пользователь видит текущие параметры, кнопкой
    «✏️ Изменить» открывает меню блоков, правит нужный и возвращается
    к сводке. Сохранение — «✅ Подтвердить».
    """
    st = {"st": "summary", "draft": _draft_from_prev(tracking),
          "ui": {}, "edit": True, "last": tracking, "editmenu": 0}
    _save(db, crypto, uid, st)
    m = summary_msg(st)
    m["text"] = ("✏️ <b>Изменение отслеживания</b> — текущие параметры:\n\n"
                 + m["text"]
                 + "\n\nНажмите «✏️ Изменить», выберите блок и внесите "
                   "правки — после правки вернётесь к этой сводке. "
                   "Если менять ничего не нужно — «🚪 Выйти».")
    return m


# ---------------------------------------------------------------- города

def _city_input_text(st, what: str) -> str:
    return (_head(st, f"Город {'вылета' if what == 'cf' else 'прилёта'}")
            + "Введите город (или аэропорт) "
            + ("вылета" if what == "cf" else "прилёта")
            + " — например «Москва» или «Шереметьево». Опечатки не "
              "страшны; если городов-однофамильцев несколько — добавьте "
              "страну: «Мельбурн, Австралия».")


def _place_step(db, crypto, uid, st, text, field, what):
    cands, more, error = places.search(text)
    if error:
        return [_msg("🛰 Сервис поиска городов недоступен, попробуйте "
                     "позже. Или введите название ещё раз.")]
    if not cands:
        return [_msg("🤷 Не нашёл. Проверьте название и повторите ввод "
                     "(город или аэропорт, можно с опечатками).")]
    st["cand"] = cands
    st["pick_for"] = what
    st["st"] = "city_pick"
    _save(db, crypto, uid, st)
    rows = [[{"text": c["label"][:60],
              "callback_data": f"d|pick|{i}"}]
            for i, c in enumerate(cands)]
    rows.append([{"text": "✍️ Ввести другое название",
                  "callback_data": f"d|research|{what}"}])
    body = "Выберите вариант кнопкой:" if len(cands) > 1 \
        else "Если вариант подходит — нажмите его:"
    if more:
        body += ("\n\nПоказал лучшие 5 — если нужного пункта нет, "
                 "уточните название (буквы или страна через запятую).")
    return [_msg(_head(st, "Город вылета" if what == "cf"
                       else "Город прилёта") + body,
                 {"inline_keyboard": rows})]


def _h_city_from(db, crypto, uid, st, text):
    return _place_step(db, crypto, uid, st, text, "origin", "cf")


def _h_city_to(db, crypto, uid, st, text):
    return _place_step(db, crypto, uid, st, text, "dest", "ct")


def _city_picked(db, crypto, uid, st, idx: int):
    cands = st.get("cand") or []
    if not (0 <= idx < len(cands)):
        return {"msgs": [_msg("Вариант не найден — выберите кнопкой.")],
                "toast": "Выберите вариант из списка", "edit": False}
    c = cands[idx]
    field = "origin" if st["pick_for"] == "cf" else "dest"
    what = "город вылета" if field == "origin" else "город прилёта"
    st["draft"][field] = c["code"]
    st["draft"][field + "_name"] = c["label"]
    st["cand"] = None
    st["pick_for"] = None
    echo = _ok(f"{what} — {c['label']}")
    if st.get("editmenu"):    # правка из меню блоков: назад к сводке
        _save(db, crypto, uid, st)
        return _to_summary(db, crypto, uid, st, echo)
    if field == "origin":
        st["st"] = "city_to"
        _save(db, crypto, uid, st)
        kb = None
        if st.get("edit") and _cur(st, "dest_name"):
            kb = ui_skip("ct", f"Оставить «{_short_name(_cur(st, 'dest_name'))}»")
        return {"msgs": [_msg(echo + "\n\n" + _city_input_text(st, "ct"),
                              kb)], "edit": True}
    st["st"] = "trip"
    _save(db, crypto, uid, st)
    return {"msgs": [_msg(echo + "\n\n" + _q_trip(st), _trip_kb(st))],
            "edit": True}


# ------------------------------------------------------------------- тип

def _q_trip(st) -> str:
    return (_head(st, "Тип поездки")
            + "Какие билеты искать? Билеты туда-обратно берутся одной "
              "покупкой, поэтому отсечка и цель — одни на поездку.")


def _trip_kb(st) -> dict:
    rows = [[{"text": "✈️ Только туда", "callback_data": "d|trip|1"},
             {"text": "🎫 Туда и обратно", "callback_data": "d|trip|2"}]]
    if st.get("edit") and _cur(st, "oneway") is not None:
        rows.append([{"text": f"⏭ Оставить ({_trip_word(_cur(st, 'oneway'))})",
                      "callback_data": "d|keep|trip"}])
    return {"inline_keyboard": rows}


# ------------------------------------------------------ переходы блоков

def _to_summary(db, crypto, uid, st, echo: str) -> dict:
    """Конец блока в режиме правки: возврат к проверке параметров."""
    st["editmenu"] = 0
    st["st"] = "summary"
    st.pop("ui", None)
    _save(db, crypto, uid, st)
    m = summary_msg(st)
    m["text"] = echo + "\n\n" + m["text"]
    return {"msgs": [m], "edit": True}


def _horizon_note(hi) -> str:
    """Пояснение про лимит продажи 400 дней (✖ в календаре)."""
    return (f"\n✖ — билеты ещё не продаются: выбор доступен максимум на "
            f"{_MAX_HORIZON_DAYS} дней вперёд, до <b>{_ddw(hi)}</b> "
            "включительно.")


def _q_out_date(st) -> dict:
    lo, hi = _cal_bounds("out", st)
    if "y" not in (st.get("ui") or {}):
        y, m = _cal_default_month(st, "out")
        st.setdefault("ui", {})["y"], st["ui"]["m"] = y, m
    kb = _cal_kb(st, "out", lo, hi)
    text = (_head(st, "Выбор даты полёта «туда»")
            + "Выберите дату вылета в календаре (листайте месяцы и годы "
              "стрелками). 🔴 — выходные и праздники производственного "
              "календаря РФ."
            + _horizon_note(hi))
    return _msg(text, kb)


def _cal_kb(st, scope: str, lo, hi) -> dict:
    """Клавиатура календаря блока + кнопки «оставить» (edit/отсечка).

    Для out/ret навигация разрешена чуть дальше горизонта (nav_hi),
    чтобы даты за лимитом 400 дней были видны в календаре (✖).
    """
    nav_hi = hi + timedelta(days=62) if scope in ("out", "ret") else hi
    kb = ui.month_kb(scope, st["ui"].get("y", lo.year),
                     st["ui"].get("m", lo.month), lo, hi,
                     selected=_cal_selected(st, scope), nav_hi=nav_hi)
    rows = kb["inline_keyboard"]
    if scope == "cut":
        prop = _cutoff_proposal(st)
        rows.append([{"text": f"✅ Оставить {_ddw(prop)}",
                      "callback_data": "d|cut|keep"}])
        if st.get("edit") and _cur(st, "cutoff"):
            rows.append([{"text": f"⏭ Оставить прежнюю "
                                  f"({_ddw(_cur(st, 'cutoff'))}) и цену",
                          "callback_data": "d|keep|cut"}])
    elif scope == "out" and st.get("edit") and \
            (_cur(st, "out") or {}).get("from"):
        rows.append([{"text": f"⏭ Оставить дату "
                              f"{_ddw(_cur(st, 'out')['from'])}",
                      "callback_data": "d|keep|outdate"}])
    elif scope == "ret" and st.get("edit") and \
            (_cur(st, "ret") or {}).get("from"):
        rows.append([{"text": f"⏭ Оставить дату "
                              f"{_ddw(_cur(st, 'ret')['from'])}",
                      "callback_data": "d|keep|retdate"}])
    return kb


def _days_kb(wkey: str, st) -> dict:
    """Кнопки-диапазоны: «только <дата>», «<дата> – <дата+1>» и т.д."""
    w = _cur(st, wkey) or {}
    d0 = date.fromisoformat(w["from"]) if w.get("from") else date.today()
    rows = []
    for n in (1, 2, 3, 4):
        d1 = d0 + timedelta(days=n - 1)
        lbl = (f"только {_ddw(d0)}" if n == 1
               else f"{_ddw(d0)} – {_ddw(d1)}")
        rows.append([{"text": lbl, "callback_data": f"d|days|{wkey}|{n}"}])
    if st.get("edit") and w.get("days"):
        blk = f"{wkey}days"
        rows.append([{"text": f"⏭ Оставить {_win_range(w)}",
                      "callback_data": f"d|keep|{blk}"}])
    return {"inline_keyboard": rows}


def _q_days(st, wkey) -> dict:
    noun = "вылета" if wkey == "out" else "возврата"
    w = _cur(st, wkey) or {}
    d0 = _ddw(w["from"]) if w.get("from") else ""
    text = (_head(st, f"Гибкая настройка времени {noun}")
            + f"Ваша дата {noun}: <b>{d0}</b>.\n"
              "Выберите, насколько можно сдвинуть дату, — бот будет "
              "следить за всеми датами выбранного периода.")
    return _msg(text, _days_kb(wkey, st))


def _time_ask_kb(wkey: str, st) -> dict:
    rows = [[{"text": "⏭ Весь день (00:00–23:59)",
              "callback_data": f"d|tall|{wkey}"}],
            [{"text": "⚙️ Настроить время суток",
              "callback_data": f"d|tset|{wkey}"}]]
    w = _cur(st, wkey) or {}
    if st.get("edit") and w.get("t1"):
        rows.append([{"text": f"⏭ Оставить ({w.get('t1')}–{w.get('t2')})",
                      "callback_data": f"d|keep|{wkey}time"}])
    return {"inline_keyboard": rows}


def _q_time_ask(st, wkey) -> dict:
    noun = "вылета" if wkey == "out" else "возврата"
    return _msg(_head(st, f"Время {noun}")
                + "Время суток: по умолчанию рассматриваются все рейсы "
                  "суток (с 00:00 до 23:59). Можно оставить как есть или "
                  "настроить границы.",
                _time_ask_kb(wkey, st))


_CLK_SCOPE = {"out_clk1": "out_t1", "out_clk2": "out_t2",
              "ret_clk1": "ret_t1", "ret_clk2": "ret_t2",
              "dig_clk": "dig_time"}
_CLK_DRAFT = {"out_t1": ("out", "t1"), "out_t2": ("out", "t2"),
              "ret_t1": ("ret", "t1"), "ret_t2": ("ret", "t2"),
              "dig_time": (None, "digest_time")}
_CLK_DEFAULT = {"out_t1": "0000", "out_t2": "2359",
                "ret_t1": "0000", "ret_t2": "2359", "dig_time": "0900"}
_CLK_TITLE = {"out_t1": "время вылета «не ранее, чем»",
              "out_t2": "время вылета «не позднее, чем»",
              "ret_t1": "время возврата «не ранее, чем»",
              "ret_t2": "время возврата «не позднее, чем»",
              "dig_time": "время дайджеста (по МСК)"}


def _clk_prefill(st, scope: str) -> str:
    wkey, tkey = _CLK_DRAFT[scope]
    if wkey:
        cur = (_cur(st, wkey) or {}).get(tkey)
    else:
        cur = _cur(st, tkey)
    if cur:
        return cur.replace(":", "")
    return _CLK_DEFAULT[scope]


def _open_clock(st, scope: str) -> dict:
    ui_ = st.setdefault("ui", {})
    ui_.pop("buf", None)
    ui_.pop("clk", None)
    _clk_state(st, scope)          # инициализация полей из предзаполнения
    st["st"] = {"out_t1": "out_clk1", "out_t2": "out_clk2",
                "ret_t1": "ret_clk1", "ret_t2": "ret_clk2",
                "dig_time": "dig_clk"}[scope]
    return _q_clock(st, scope)


def _clk_state(st, scope: str) -> dict:
    """Состояние часов {"h","m","f"}; совместимо со старым buf-строкой."""
    ui_ = st.setdefault("ui", {})
    c = ui_.get("clk")
    if isinstance(c, dict) and "h" in c and "m" in c and "f" in c:
        c["f"] = c["f"] if c["f"] in ("h", "m") else "h"
        return c
    buf = str(ui_.get("buf") or "")
    if not buf:
        buf = _clk_prefill(st, scope)
    buf = "".join(ch for ch in buf if ch.isdigit())[:4]
    c = {"h": buf[:2], "m": buf[2:4],
         "f": ("m" if len(buf) >= 2 else "h") if ui_.get("buf") else "h"}
    ui_["clk"] = c
    ui_.pop("buf", None)
    return c


def _q_clock(st, scope: str) -> dict:
    c = _clk_state(st, scope)
    body = (f"{_CLK_TITLE[scope]}: {ui.clock_face(c)}\n"
            "Набирайте цифры: после двух цифр ввод переходит к минутам; "
            "если поле заполнено, новая цифра затирает его и набор "
            "начинается заново. Тап по [часы]/[минуты] переключает поле. "
            "Или отправьте время текстом: «1057» или «10:57» — целиком; "
            "«9» или «37» — записать в активное поле.")
    return _msg(_head(st, "Время") + body, ui.clock_kb(scope, c))


def _clock_ok(st, scope: str, hhmm: str):
    """Сохранение времени; возвращает (ошибка|None, не_зависимые_сообщения)."""
    wkey, tkey = _CLK_DRAFT[scope]
    if wkey:
        st["draft"].setdefault(wkey, {})[tkey] = hhmm
    else:
        st["draft"][tkey] = hhmm
    return None


def _t2_relation_error(st, wkey: str, hhmm: str) -> str | None:
    w = _cur(st, wkey) or {}
    if int(w.get("days") or 1) == 1 and hhmm <= w.get("t1", "00:00"):
        return (f"Время «не позднее» должно быть больше, чем {w.get('t1')} "
                f"(в тот же единственный день).")
    return None


# ------------------------------------------------- блоки 4–5: окна дат

def _h_cal(db, crypto, uid, st, scope: str, arg: str):
    lo, hi = _cal_bounds(scope, st)
    if len(arg) == 7:  # навигация YYYY-MM: только клавиатура меняется
        y, m = int(arg[:4]), int(arg[5:7])
        st["ui"]["y"], st["ui"]["m"] = y, m
        _save(db, crypto, uid, st)
        return {"msgs": [], "kb_only": _cal_kb(st, scope, lo, hi),
                "edit": True}
    try:
        d = date.fromisoformat(arg)
    except ValueError:
        return {"msgs": [_msg("Некорректная дата.")], "edit": False}
    if not (lo <= d <= hi):
        if scope in ("out", "ret") and d > hi:
            opens = d - timedelta(days=_MAX_HORIZON_DAYS)
            return {"msgs": [], "edit": False,
                    "toast": f"Билеты продаются максимум на {_MAX_HORIZON_DAYS} "
                             f"дней вперёд — дата {_ddw(d)} откроется "
                             f"{_ddw(opens)}."}
        return {"msgs": [], "toast": "Дата вне допустимого интервала",
                "edit": False}
    if scope == "out":
        w = st["draft"].get("out") or {}
        st["draft"]["out"] = {"from": d.isoformat(), "days": w.get("days"),
                              "t1": w.get("t1"), "t2": w.get("t2")}
        st["st"] = "out_days"
        _save(db, crypto, uid, st)
        echo = _ok(f"дата вылета — {_ddw(d)}")
        m = _q_days(st, "out")
        m["text"] = echo + "\n\n" + m["text"]
        return {"msgs": [m], "edit": True}
    if scope == "ret":
        w = st["draft"].get("ret") or {}
        st["draft"]["ret"] = {"from": d.isoformat(), "days": w.get("days"),
                              "t1": w.get("t1"), "t2": w.get("t2")}
        st["st"] = "ret_days"
        _save(db, crypto, uid, st)
        echo = _ok(f"дата возврата — {_ddw(d)}")
        m = _q_days(st, "ret")
        m["text"] = echo + "\n\n" + m["text"]
        return {"msgs": [m], "edit": True}
    # cut
    st["draft"]["cutoff"] = d.isoformat()
    _save(db, crypto, uid, st)
    return {"msgs": [], "edit": True, "fetch": True,
            "echo": _ok(f"дата отсечки — {_ddw(d)}"),
            "draft": st["draft"]}


def _h_days(db, crypto, uid, st, wkey: str, n: int):
    w = st["draft"].get(wkey) or {}
    w["days"] = n
    st["draft"][wkey] = w
    st["st"] = f"{wkey}_time_ask"
    _save(db, crypto, uid, st)
    noun = "вылета" if wkey == "out" else "возврата"
    what = (_ddw(_win_dates(w)[0]) if n == 1 else
            f"период {noun} ({_days_word(n)}) — {_win_range(w)}")
    echo = _ok(what)
    m = _q_time_ask(st, wkey)
    m["text"] = echo + "\n\n" + m["text"]
    return {"msgs": [m], "edit": True}


def _h_tall(db, crypto, uid, st, wkey: str):
    w = st["draft"].setdefault(wkey, {})
    w["t1"], w["t2"] = "00:00", "23:59"
    noun = "вылета" if wkey == "out" else "возврата"
    echo = _ok(f"время {noun} — весь день (00:00–23:59)")
    if st.get("editmenu"):
        _save(db, crypto, uid, st)
        return _to_summary(db, crypto, uid, st, echo)
    return _finish_window_block(db, crypto, uid, st, wkey, echo)


def _finish_window_block(db, crypto, uid, st, wkey: str, echo: str) -> dict:
    """Конец блока окна: вылет → (возврат | отсечка), возврат → отсечка."""
    _save(db, crypto, uid, st)
    if st.get("editmenu"):
        return _to_summary(db, crypto, uid, st, echo)
    if wkey == "out" and not _cur(st, "oneway"):
        st["st"] = "ret_date"
        st.pop("ui", None)
        _save(db, crypto, uid, st)
        m = _q_ret_date(st)
        m["text"] = echo + "\n\n" + m["text"]
        return {"msgs": [m], "edit": True}
    st["st"] = "cutoff"
    st.pop("ui", None)
    _save(db, crypto, uid, st)
    m = _q_cutoff(st)
    m["text"] = echo + "\n\n" + m["text"]
    return {"msgs": [m], "edit": True}


def _q_ret_date(st) -> dict:
    lo, hi = _cal_bounds("ret", st)
    if "y" not in (st.get("ui") or {}):
        out = _cur(st, "out") or {}
        base = (_cur(st, "ret") or {}).get("from")
        if not base and out.get("from"):
            base = _win_dates(out)[1].isoformat()
        d = date.fromisoformat(base) if base else date.today()
        st.setdefault("ui", {})["y"], st["ui"]["m"] = d.year, d.month
    kb = _cal_kb(st, "ret", lo, hi)
    text = (_head(st, "Выбор даты полёта «обратно»")
            + "Выберите дату возврата (не раньше окончания периода "
              "вылета). 🔴 — выходные и праздники."
            + _horizon_note(hi))
    return _msg(text, kb)


# ------------------------------------------------ блок 6: отсечка+цена

def _cutoff_proposal(st) -> date:
    out = _cur(st, "out") or {}
    dep = date.fromisoformat(out["from"]) if out.get("from") else \
        date.today() + timedelta(days=14)
    return max(date.today(), dep - timedelta(days=14))


def _q_cutoff(st) -> dict:
    prop = _cutoff_proposal(st)
    lo, hi = _cal_bounds("cut", st)
    if "y" not in (st.get("ui") or {}):
        st.setdefault("ui", {})["y"], st["ui"]["m"] = prop.year, prop.month
    kb = _cal_kb(st, "cut", lo, hi)
    text = (_head(st, "Отсечка и целевая цена")
            + "<b>Дата отсечки</b> — до какой даты билет нужно купить "
              "в любом случае (даже если цена не упадёт). Предлагаю "
              f"<b>{_ddw(prop)}</b> — за 14 дней до даты вылета, но не "
              "раньше сегодняшнего дня. Выберите другую дату в календаре "
              "или оставьте предложенную.")
    return _msg(text, kb)


def _h_cutoff(db, crypto, uid, st, keep: bool):
    prev = _cur(st, "cutoff")
    st["draft"]["cutoff"] = (prev if (keep and prev)
                             else _cutoff_proposal(st).isoformat())
    _save(db, crypto, uid, st)
    return {"msgs": [], "edit": True, "fetch": True,
            "echo": _ok(f"дата отсечки — {_ddw(st['draft']['cutoff'])}",
                        kept=bool(keep and prev)),
            "draft": st["draft"]}


def price_ready(db, crypto, uid: str, min_price: int | None) -> dict:
    """UI целевой цены после поиска (одного запроса к кэшу Aviasales)."""
    st = _load(crypto, db.get_user(uid))
    st["st"] = "target"
    suggest = None
    if min_price:
        suggest = max(50, round(min_price * 0.85 / 50) * 50)
    st.setdefault("ui", {})
    st["ui"]["minp"], st["ui"]["suggest"] = min_price, suggest
    _save(db, crypto, uid, st)
    return _q_target(st)


def _q_target(st) -> dict:
    minp = (st.get("ui") or {}).get("minp")
    sug = (st.get("ui") or {}).get("suggest")
    if minp:
        body = (f"Минимальная цена в кэше Aviasales по вашему маршруту "
                f"сейчас: <b>{_rub(minp)}</b>.\n"
                f"Предлагаю целевую цену <b>{_rub(sug)}</b> — на 15% ниже "
                "минимума: как только цена станет равна или ниже цели, "
                "пришлю мгновенное уведомление.")
    else:
        body = ("Не смог получить текущие цены (кэш недоступен). "
                "Задайте целевую цену вручную или продолжите без неё — "
                "уведомления по цене будут выключены, дайджест продолжит "
                "приходить.")
    rows = []
    if sug:
        rows.append([{"text": f"✅ {_rub(sug)} (−15%)",
                      "callback_data": "d|tgt|suggest"}])
    rows.append([{"text": "⏭ Без целевой цены", "callback_data": "d|tgt|off"},
                 {"text": "✍️ Своя цена", "callback_data": "d|tgt|manual"}])
    if st.get("edit") and _cur(st, "target") is not None:
        rows.append([{"text": f"⏭ Оставить прежнюю "
                              f"({_target_word(_cur(st, 'target'))})",
                      "callback_data": "d|keep|target"}])
    return _msg(_head(st, "Отсечка и целевая цена") + body,
                {"inline_keyboard": rows})


# --------------------------------------------------- блок 7: дайджест

def _q_digest(st) -> dict:
    ev = _cur(st, "digest_every") or 1
    tm = _cur(st, "digest_time") or "09:00"
    freq = [("1 день", 1), ("3 дня", 3), ("7 дней", 7), ("14 дней", 14)]
    rows = [[{"text": ("✅ " if ev == n else "") + lbl,
              "callback_data": f"d|freq|{n}"}] for lbl, n in freq]
    rows.append([{"text": f"⏰ Время: {tm} (изменить)",
                  "callback_data": "d|clkask|dig_time"}])
    rows.append([{"text": "✅ Подтвердить", "callback_data": "d|sum|back"}])
    text = (_head(st, "Дайджест")
            + "Сводка по ценам — в этот же чат. Частота отсчитывается от "
              f"даты запуска отслеживания. Выбрано: {_every_word(ev)}, "
              f"время {tm} МСК. По умолчанию — ежедневно в 09:00; можно "
              "менять кнопками и сразу подтвердить.")
    return _msg(text, {"inline_keyboard": rows})


# ------------------------------------------------------------ сводка

def _summary_text(t: dict) -> str:
    out = ["📋 <b>Проверьте параметры:</b>",
           f"1. Откуда: {t['origin_name']}",
           f"2. Куда: {t['dest_name']}",
           f"3. Тип: {_trip_word(t.get('oneway'))}"]
    w = t.get("out") or {}
    if w.get("from"):
        d0, d1 = _win_dates(w)
        win = (f"{_ddw(d0)}, {w.get('t1')}–{w.get('t2')}"
               if int(w.get("days") or 1) == 1 else
               f"{_ddw(d0)} ({w.get('t1')}) – "
               f"{_ddw(d1)} ({w.get('t2')})")
        out.append(f"4. Полёт «туда»: {win}")
    r = t.get("ret")
    if not t.get("oneway") and r and r.get("from"):
        d0, d1 = _win_dates(r)
        win = (f"{_ddw(d0)}, {r.get('t1')}–{r.get('t2')}"
               if int(r.get("days") or 1) == 1 else
               f"{_ddw(d0)} ({r.get('t1')}) – "
               f"{_ddw(d1)} ({r.get('t2')})")
        out.append(f"5. Полёт «обратно»: {win}")
    out.append(f"6. Дата отсечки (купить обязательно): {_ddw(t['cutoff'])}")
    out.append(f"   Целевая цена: {_target_word(t.get('target'))}")
    out.append(f"7. Дайджест: {_every_word(t.get('digest_every', 1))} в "
               f"{t.get('digest_time', '09:00')} МСК")
    return "\n".join(out)


def summary_msg(st) -> dict:
    t = dict(st["draft"])
    t.setdefault("digest_every", 1)
    t.setdefault("digest_time", "09:00")
    rows = [[{"text": "✅ Подтвердить", "callback_data": "d|sum|yes"}],
            [{"text": "✏️ Изменить", "callback_data": "d|sum|edit"}]]
    rows.append([{"text": ("🚪 Выйти без изменений" if st.get("edit")
                           else "🚪 Отменить настройку"),
                  "callback_data": "d|exit"}])
    return _msg(_summary_text(t), {"inline_keyboard": rows})


def _editmenu_kb(st) -> dict:
    rows = [[{"text": "1️⃣ Город вылета", "callback_data": "d|jump|cf"},
             {"text": "2️⃣ Город прилёта", "callback_data": "d|jump|ct"}],
            [{"text": "3️⃣ Тип поездки", "callback_data": "d|jump|trip"},
             {"text": "4️⃣ Дата и время «туда»",
              "callback_data": "d|jump|out"}]]
    if not _cur(st, "oneway"):
        rows.append([{"text": "5️⃣ Дата и время «обратно»",
                      "callback_data": "d|jump|ret"}])
    rows.append([{"text": "6️⃣ Отсечка и цена", "callback_data": "d|jump|cut"},
                 {"text": "7️⃣ Дайджест", "callback_data": "d|jump|dig"}])
    rows.append([{"text": "⬅️ К проверке параметров",
                  "callback_data": "d|sum|back"}])
    return {"inline_keyboard": rows}


def _q_editmenu(st) -> dict:
    return _msg("✏️ <b>Что изменить?</b>\n\nБлок откроется с текущими "
                "значениями, после правки вернёмся к проверке.",
                _editmenu_kb(st))


def _jump(db, crypto, uid, st, blk: str):
    st["editmenu"] = 1
    st.setdefault("ui", {}).pop("buf", None)
    m = None
    if blk == "cf":
        st["st"] = "city_from"
        m = _msg(_city_input_text(st, "cf"))
    elif blk == "ct":
        st["st"] = "city_to"
        m = _msg(_city_input_text(st, "ct"))
    elif blk == "trip":
        st["st"] = "trip"
        m = _msg(_q_trip(st), _trip_kb(st))
    elif blk == "out":
        st["st"] = "out_date"
        st.pop("ui", None)
        m = _q_out_date(st)
    elif blk == "ret":
        st["st"] = "ret_date"
        st.pop("ui", None)
        m = _q_ret_date(st)
    elif blk == "cut":
        st["st"] = "cutoff"
        st.pop("ui", None)
        m = _q_cutoff(st)
    elif blk == "dig":
        st["st"] = "digest"
        m = _q_digest(st)
    _save(db, crypto, uid, st)
    return {"msgs": [m], "edit": True}


# ------------------------------------------------------------ keep-кнопки

def _keep(db, crypto, uid, st, blk: str):
    nxt = {"cf": ("city_to", "ct"), "ct": ("trip", None),
           "trip": ("out_date", None),
           "outdate": ("out_days", None), "outdays": ("out_time_ask", None),
           "outtime": ("ret_date", None),
           "retdate": ("ret_days", None), "retdays": ("ret_time_ask", None),
           "rettime": ("cutoff", None), "cut": ("digest", None),
           "target": ("digest", None), "dig": ("summary", None)}
    w_out = _cur(st, "out") or {}
    w_ret = _cur(st, "ret") or {}
    labels = {"cf": f"город вылета — {_cur(st, 'origin_name')}",
              "ct": f"город прилёта — {_cur(st, 'dest_name')}",
              "trip": f"тип поездки — {_trip_word(_cur(st, 'oneway'))}",
              "outdate": f"дата вылета — "
                         f"{_ddw((_cur(st, 'out') or {}).get('from'))}",
              "outdays": "гибкость вылета — "
                         + (_win_range(w_out) if w_out.get("from") else "—"),
              "outtime": f"время вылета — "
                         f"{w_out.get('t1')}–{w_out.get('t2')}",
              "retdate": f"дата возврата — "
                         f"{_ddw((_cur(st, 'ret') or {}).get('from'))}",
              "retdays": "гибкость возврата — "
                         + (_win_range(w_ret) if w_ret.get("from") else "—"),
              "rettime": f"время возврата — "
                         f"{w_ret.get('t1')}–{w_ret.get('t2')}",
              "cut": f"отсечка и цена — {_ddw(_cur(st, 'cutoff'))}, "
                     f"{_target_word(_cur(st, 'target'))}",
              "target": f"целевая цена — {_target_word(_cur(st, 'target'))}",
              "dig": f"дайджест — {_every_word(_cur(st, 'digest_every') or 1)} "
                     f"в {_cur(st, 'digest_time') or '09:00'} МСК"}
    state, _ = nxt[blk]
    echo = _ok(labels[blk], kept=True)
    if st.get("editmenu"):
        return _to_summary(db, crypto, uid, st, echo)
    if blk == "outtime" and not _cur(st, "oneway"):
        return _finish_window_block(db, crypto, uid, st, "out", echo)
    if blk == "outtime":
        st["st"] = "cutoff"
    else:
        st["st"] = state
    _save(db, crypto, uid, st)
    if st["st"] == "summary":
        m = summary_msg(st)
    elif st["st"] == "digest":
        m = _q_digest(st)
    elif st["st"] == "cutoff":
        m = _q_cutoff(st)
    elif st["st"] == "out_date":
        m = _q_out_date(st)
    elif st["st"] == "ret_date":
        m = _q_ret_date(st)
    elif st["st"] == "trip":
        m = _msg(_q_trip(st), _trip_kb(st))
    elif st["st"] == "out_days":
        m = _q_days(st, "out")
    elif st["st"] == "ret_days":
        m = _q_days(st, "ret")
    elif st["st"] == "out_time_ask":
        m = _q_time_ask(st, "out")
    elif st["st"] == "ret_time_ask":
        m = _q_time_ask(st, "ret")
    else:
        m = _msg(_city_input_text(st, "ct" if state == "city_to" else "cf"))
    m["text"] = echo + "\n\n" + m["text"]
    return {"msgs": [m], "edit": True}

# ------------------------------------------------------------- роутеры

def cb(db, crypto, uid: str, data: str):
    """Обработка нажатия inline-кнопки.

    Возвращает один из вариантов:
      {"msgs": [...], "toast": str|None, "edit": bool}  — обычный шаг
      {"msgs": [...], "edit": True, "fetch": True, "echo": str}
                                       — сначала поиск цены (bot.py)
      (draft, "SAVE")                  — подтверждение сводки
    """
    row = db.get_user(uid)
    st = _load(crypto, row)
    if not st:
        return {"msgs": [_msg("Отправьте /watch, чтобы настроить "
                              "отслеживание.")], "edit": False}
    st["draft"] = st.get("draft") or {}
    st.setdefault("ui", {})
    parts = (data or "").split("|")
    if parts[0] != "d" or len(parts) < 2:
        return {"msgs": [], "toast": "Неизвестная кнопка", "edit": False}
    act = parts[1]
    try:
        if act == "noop":
            return {"msgs": [], "toast": "Этот элемент неактивен",
                    "edit": False}
        if act == "pick":
            return _city_picked(db, crypto, uid, st, int(parts[2]))
        if act == "research":
            st["st"] = "city_from" if parts[2] == "cf" else "city_to"
            st["cand"] = None
            _save(db, crypto, uid, st)
            return {"msgs": [_msg(_city_input_text(st, parts[2]))],
                    "edit": True}
        if act == "trip":
            return _h_trip(db, crypto, uid, st, parts[2])
        if act == "cal":
            return _h_cal(db, crypto, uid, st, parts[2], parts[3])
        if act == "days":
            if parts[3] not in ("1", "2", "3", "4"):
                return {"msgs": [], "toast": "Выберите один из вариантов — "
                                "период от 1 до 4 дней",
                        "edit": False}
            return _h_days(db, crypto, uid, st, parts[2], int(parts[3]))
        if act == "tall":
            return _h_tall(db, crypto, uid, st, parts[2])
        if act == "tset":
            st["st"] = f"{parts[2]}_clk1"
            _save(db, crypto, uid, st)
            scope = "out_t1" if parts[2] == "out" else "ret_t1"
            return {"msgs": [_open_clock(st, scope)], "edit": True}
        if act == "clk":
            return _h_clk(db, crypto, uid, st, parts[2], parts[3])
        if act == "clkask":
            m = _open_clock(st, parts[2])
            _save(db, crypto, uid, st)
            return {"msgs": [m], "edit": True}
        if act == "cut":
            return _h_cutoff(db, crypto, uid, st, keep=True)
        if act == "tgt":
            return _h_tgt(db, crypto, uid, st, parts[2])
        if act == "freq":
            st["draft"]["digest_every"] = int(parts[2])
            st["st"] = "digest"
            _save(db, crypto, uid, st)
            return {"msgs": [_q_digest(st)], "edit": True}
        if act == "keep":
            return _keep(db, crypto, uid, st, parts[2])
        if act == "sum":
            if parts[2] == "yes":
                st["editmenu"] = 0
                draft = dict(st["draft"])
                draft.setdefault("digest_every", 1)
                draft.setdefault("digest_time", "09:00")
                st["draft"] = draft
                _save(db, crypto, uid, None)
                return draft, "SAVE"
            if parts[2] == "edit":
                return {"msgs": [_q_editmenu(st)], "edit": True}
            if parts[2] == "back":
                st["editmenu"] = 0
                st["st"] = "summary"
                _save(db, crypto, uid, st)
                return {"msgs": [summary_msg(st)], "edit": True}
        if act == "jump":
            return _jump(db, crypto, uid, st, parts[2])
        if act == "exit":
            row = db.get_user(uid)
            had = bool(row and row["tracking_enc"])
            _save(db, crypto, uid, None)
            msg = ("Изменения отменены — отслеживание работает, как "
                   "раньше. Сводка: /status." if had else
                   "Настройка прервана. /watch — начать заново.")
            return {"msgs": [_msg(msg)], "edit": True}
        if act == "stop":
            return _h_stop(db, crypto, uid, st, parts[2])
    except Exception as e:  # защита от каскада ошибок в диалоге
        log.exception("dialog cb error: %s", e)
        _save(db, crypto, uid, None)
        return {"msgs": [_msg("⚠️ Ошибка диалога, мастер перезапущен. "
                              "Отправьте /watch.")], "edit": False}
    return {"msgs": [], "toast": "Неизвестная кнопка", "edit": False}


def _h_trip(db, crypto, uid, st, val: str):
    if val == "keep":
        oneway = _cur(st, "oneway")
        echo = _ok(f"тип поездки — {_trip_word(oneway)}", kept=True)
    else:
        oneway = val == "1"
        st["draft"]["oneway"] = oneway
        if oneway:
            st["draft"].pop("ret", None)
        echo = _ok(f"тип поездки — {_trip_word(oneway)}")
    if st.get("editmenu"):
        _save(db, crypto, uid, st)
        return _to_summary(db, crypto, uid, st, echo)
    st["st"] = "out_date"
    st.pop("ui", None)
    _save(db, crypto, uid, st)
    m = _q_out_date(st)
    m["text"] = echo + "\n\n" + m["text"]
    return {"msgs": [m], "edit": True}


def _h_tgt(db, crypto, uid, st, val: str):
    if val == "suggest":
        sug = (st.get("ui") or {}).get("suggest")
        if not sug:
            return {"msgs": [], "toast": "Сначала подождите расчёт цены",
                    "edit": False}
        st["draft"]["target"] = int(sug)
        echo = _ok(f"целевая цена — {_rub(int(sug))}")
    elif val == "off":
        st["draft"]["target"] = 0
        echo = _ok("целевая цена — не задана: уведомления по цене "
                   "выключены")
    else:  # manual
        st["st"] = "target_manual"
        _save(db, crypto, uid, st)
        return {"msgs": [_msg(
            _head(st, "Отсечка и целевая цена")
            + "Введите целевую максимальную цену поездки в рублях целым "
              "числом (например 25000): как только цена станет равна или "
              "ниже — пришлю мгновенное уведомление. «выкл» — без цели.")],
            "edit": True}
    if st.get("editmenu"):
        _save(db, crypto, uid, st)
        return _to_summary(db, crypto, uid, st, echo)
    st["st"] = "digest"
    _save(db, crypto, uid, st)
    m = _q_digest(st)
    m["text"] = echo + "\n\n" + m["text"]
    return {"msgs": [m], "edit": True}


def _h_clk(db, crypto, uid, st, scope: str, tok: str):
    """Кнопки часов: поля [ЧЧ]/[ММ] (переключение), цифра, ⌫, Готово."""
    c = _clk_state(st, scope)
    if tok in ("fh", "fm"):            # тап по полю — переключить его
        c["f"] = tok[1]
        _save(db, crypto, uid, st)
        return {"msgs": [_q_clock(st, scope)], "edit": True}
    if tok == "del":
        f = c["f"]
        c[f] = (c[f] or "")[:-1]
        if not c[f]:
            c["f"] = "h"
        _save(db, crypto, uid, st)
        return {"msgs": [_q_clock(st, scope)], "edit": True}
    if tok == "ok":
        if len(c["h"]) < 2 or len(c["m"]) < 2:
            return {"msgs": [], "toast": "Заполните обе части: часы и минуты",
                    "edit": False}
        hhmm = f"{c['h']}:{c['m']}"
        if not parse_time(hhmm):
            c["h"] = c["m"] = ""
            c["f"] = "h"
            _save(db, crypto, uid, st)
            return {"msgs": [_q_clock(st, scope)],
                    "toast": f"Некорректное время: {hhmm}. Введите заново",
                    "edit": True}
        wkey, _ = _CLK_DRAFT[scope]
        if wkey and scope.endswith("t2"):
            err = _t2_relation_error(st, wkey, hhmm)
            if err:
                c["h"] = c["m"] = ""
                c["f"] = "h"
                _save(db, crypto, uid, st)
                return {"msgs": [], "toast": err + " Введите время заново",
                        "edit": False}
        _clock_ok(st, scope, hhmm)
        return _clock_next(db, crypto, uid, st, scope, hhmm)
    # цифра: в заполненное поле — затирание (поле начинается заново)
    f = c["f"]
    cur = c[f] or ""
    if len(cur) >= 2:
        cur = ""
    cur += tok
    if len(cur) == 2 and int(cur) > (23 if f == "h" else 59):
        c[f] = ""
        _save(db, crypto, uid, st)
        return {"msgs": [_q_clock(st, scope)],
                "toast": ("Часы — от 00 до 23" if f == "h"
                          else "Минуты — от 00 до 59"),
                "edit": True}
    c[f] = cur
    if len(cur) == 2 and f == "h":
        c["f"] = "m"          # авто-переход к минутам
    _save(db, crypto, uid, st)
    return {"msgs": [_q_clock(st, scope)], "edit": True}


def _clock_next(db, crypto, uid, st, scope: str, hhmm: str):
    what = _CLK_TITLE[scope]
    echo = _ok(f"{what} — {hhmm}")
    if scope in ("out_t1", "ret_t1"):
        nxt = "out_clk2" if scope == "out_t1" else "ret_clk2"
        nxt_scope = "out_t2" if scope == "out_t1" else "ret_t2"
        st["st"] = nxt
        st.setdefault("ui", {}).pop("buf", None)
        st["ui"].pop("clk", None)      # чистые поля: предзаполнение t2
        _clk_state(st, nxt_scope)
        _save(db, crypto, uid, st)
        m = _q_clock(st, nxt_scope)
        m["text"] = echo + "\n\n" + m["text"]
        return {"msgs": [m], "edit": True}
    if scope == "out_t2":
        return _finish_window_block(db, crypto, uid, st, "out", echo)
    if scope == "ret_t2":
        return _finish_window_block(db, crypto, uid, st, "ret", echo)
    # dig_time -> блок дайджеста (или сводка в режиме правки)
    if st.get("editmenu"):
        st["editmenu"] = 0
        st["st"] = "summary"
        _save(db, crypto, uid, st)
        m = summary_msg(st)
    else:
        st["st"] = "digest"
        _save(db, crypto, uid, st)
        m = _q_digest(st)
    m["text"] = echo + "\n\n" + m["text"]
    return {"msgs": [m], "edit": True}


def _h_stop(db, crypto, uid, st, val: str):
    from . import users as U
    if val == "yes":
        prev = U.remove_tracking(db, crypto, uid)
        _save(db, crypto, uid,
              {"st": "stopped", "last": prev} if prev else None)
        if prev:
            return {"msgs": [_msg("🗑 Отслеживание удалено.\n\nНастроить "
                                  "новое: /watch — прежние значения "
                                  "подставлю.")], "edit": True}
        return {"msgs": [_msg("🗑 Отслеживание удалено. Настроить новое: "
                              "/watch")], "edit": True}
    _save(db, crypto, uid, None)
    return {"msgs": [_msg("Отменено — отслеживание продолжает "
                          "работать.")], "edit": True}


# ------------------------------------------------------ текстовый ввод

def feed(db, crypto, uid: str, text: str):
    """Текстовое сообщение во время диалога (названия городов, цена,
    время «1057», «да/нет» у удаления). Возвращает список сообщений
    (dict с text/kb) или (draft, "SAVE")."""
    row = db.get_user(uid)
    st = _load(crypto, row)
    if not st:
        return [_msg("Отправьте /watch, чтобы настроить отслеживание.")]
    st["draft"] = st.get("draft") or {}
    st.setdefault("ui", {})
    state = st.get("st")
    try:
        if state == "stop_confirm":
            t = (text or "").strip().lower()
            if t in ("да", "yes", "y"):
                return _pack(_h_stop(db, crypto, uid, st, "yes"))
            if t in ("нет", "no", "n"):
                return _pack(_h_stop(db, crypto, uid, st, "no"))
            return [_msg("Нажмите кнопку: «🗑 Да, удалить» или "
                         "«✖️ Нет» (или ответьте да/нет).")]
        if state in ("city_from", "city_to"):
            what = "cf" if state == "city_from" else "ct"
            return _pack(*_place_step(db, crypto, uid, st, text,
                                      "origin" if what == "cf" else "dest",
                                      what))
        if state == "city_pick":
            cands = st.get("cand") or []
            if not (text or "").strip().isdigit() or \
                    not (1 <= int(text) <= len(cands)):
                return [_msg(f"Введите номер от 1 до {len(cands)} или "
                             f"выберите кнопкой.")]
            return _pack(_city_picked(db, crypto, uid, st, int(text) - 1))
        if state in ("out_clk1", "out_clk2", "ret_clk1", "ret_clk2",
                     "dig_clk"):
            scope = _CLK_SCOPE[state]
            c = _clk_state(st, scope)
            s = (text or "").strip()
            t = parse_time_text(s)
            if t:
                wkey, _ = _CLK_DRAFT[scope]
                if wkey and scope.endswith("t2"):
                    err = _t2_relation_error(st, wkey, t)
                    if err:
                        return [_msg(err + " Повторите ввод.")]
                _clock_ok(st, scope, t)
                return _pack(_clock_next(db, crypto, uid, st, scope, t))
            if s.isdigit() and 1 <= len(s) <= 2:
                # затирание активного поля: «9» -> 09, «37» -> 37
                v = s.zfill(2)
                f = c["f"]
                lim = 23 if f == "h" else 59
                if int(v) > lim:
                    return [_msg(f"{'Часы' if f == 'h' else 'Минуты'} — "
                                 f"от 00 до {lim}. Повторите ввод.")]
                c[f] = v
                if f == "h":
                    c["f"] = "m"
                _save(db, crypto, uid, st)
                return [_q_clock(st, scope)]
            return [_msg("Не понял время. Отправьте «1057» или «10:57» "
                         "(целиком), либо 1–2 цифры — записать в активное "
                         "поле (часы 00–23, минуты 00–59).")]
        if state == "target_manual":
            t = (text or "").strip().lower()
            if t in ("выкл", "0"):
                st["draft"]["target"] = 0
                echo = _ok("целевая цена — не задана: уведомления по цене "
                           "выключены")
            elif t.isdigit() and 1 <= int(t) <= 10_000_000:
                st["draft"]["target"] = int(t)
                echo = _ok(f"целевая цена — {_rub(int(t))}")
            else:
                return [_msg("Введите целое число рублей (например "
                             "25000) или «выкл» — без целевой цены.")]
            if st.get("editmenu"):
                st["editmenu"] = 0
                st["st"] = "summary"
                _save(db, crypto, uid, st)
                m = summary_msg(st)
            else:
                st["st"] = "digest"
                _save(db, crypto, uid, st)
                m = _q_digest(st)
            m["text"] = echo + "\n\n" + m["text"]
            return [m]
        if state == "wl":
            return [_msg("⏳ Вы в листе ожидания — параметры сохранены. "
                         "Как только место освободится, пришлю сообщение "
                         "(или попробуйте /retry).")]
        if state == "stopped":
            return [_msg("Отслеживание удалено. Настроить новое: /watch "
                         "(прежние параметры подставлю). Справка: /help.")]
        # любые другие состояния: текст не ожидался
        return [_msg("Здесь выбирайте кнопки под сообщением. "
                     "Для перезапуска: /watch.")]
    except Exception as e:  # защита от каскада ошибок в диалоге
        log.exception("dialog error: %s", e)
        _save(db, crypto, uid, None)
        return [_msg("⚠️ Ошибка диалога, мастер перезапущен. "
                     "Отправьте /watch.")]


def _pack(*items):
    """cb-результаты -> список сообщений (или SAVE-сигнал)."""
    out = []
    for it in items:
        if it is None:
            continue
        if isinstance(it, tuple) and len(it) == 2 and it[1] == "SAVE":
            return it
        if isinstance(it, dict) and "msgs" in it:
            out.extend(it["msgs"])
        else:
            out.append(it)
    return out


# ---------------------------------------------------- служебные состояния

def request_stop(db, crypto, uid: str) -> dict:
    _save(db, crypto, uid, {"st": "stop_confirm", "draft": {}, "ui": {}})
    return _msg("🚫 Удалить отслеживание полностью?",
                {"inline_keyboard": [
                    [{"text": "🗑 Да, удалить", "callback_data": "d|stop|yes"},
                     {"text": "✖️ Нет, оставить",
                      "callback_data": "d|stop|no"}]]})


def save_waitlist_draft(db, crypto, uid: str, draft: dict):
    _save(db, crypto, uid, {"st": "wl", "draft": draft, "ui": {}})


def try_retry(db, crypto, uid: str, admin_uid_str: str, cfg_mu: dict) -> str:
    """Активация сохранённых параметров, когда появилось место."""
    row = db.get_user(uid)
    st = _load(crypto, row)
    if st.get("st") != "wl" or not st.get("draft"):
        return "none"
    from . import users as U
    res = U.save_tracking(db, crypto, uid, admin_uid_str, st["draft"],
                          cfg_mu)
    if res == "ok":
        _save(db, crypto, uid, None)
    return res


def finish_save(db, crypto, uid, admin_uid_str: str, draft: dict,
                cfg_mu: dict) -> str:
    """Активация после подтверждения: 'ok' | 'waitlist'."""
    from . import users as U
    return U.save_tracking(db, crypto, uid, admin_uid_str, draft, cfg_mu)

