"""Диалог настройки отслеживания (визард). Ревизия логики 2026-10.

Что изменилось по сравнению с «9 шагами с нулями-умолчаниями»:
- Шаги нумеруются «N/15» для «туда и обратно». При выборе «только туда»
  шаги окна возврата (8–11) пропускаются, нумерация пересчитывается:
  отсечка становится 8-м шагом из 11, целевая цена — 9-м и т.д.
  До выбора типа поездки показывается максимум: «1/15», «2/15», «3/15».
- Значений по умолчанию больше нет: даты, длительности окон, тип
  поездки и частота дайджеста вводятся явно. Остались ЯВНЫЕ шорткаты:
  «0» в вопросах времени — 00:00 / 23:59; «0» в вопросе целевой цены —
  выключить уведомления по цене.
- После КАЖДОГО принятого значения бот подтверждает, что записал
  («✅ Принято: …»), и только потом задаёт следующий вопрос.
- Выбор города/аэропорта ВСЕГДА идёт через меню вариантов — даже когда
  совпадение одно («все аэропорты» + конкретные аэропорты).
- Режим изменения (/stop → /watch): в каждом вопросе показывается
  текущее (прежнее) значение; «0» — оставить его.
- Контроль ввода: дата вылета — не раньше сегодняшней и не дальше
  ~13 месяцев; окно возврата — не раньше окончания окна вылета; дата
  отсечки — от сегодняшнего дня до начала окна вылета (единое правило
  для «только туда» и «туда и обратно»: билеты туда-обратно берутся
  одной покупкой, отдельная отсечка для возврата не нужна); время —
  ЧЧ:ММ с проверкой диапазонов.

Состояние диалога шифруется (dialog_enc) и живёт в БД — каждый запуск
workflow это новый процесс, поэтому State Machine обязана быть внешней.
/cancel прерывает мастер без сохранения.
"""
import logging
from datetime import date, timedelta

from . import places

log = logging.getLogger(__name__)

# Порядковые номера шагов («туда и обратно»). В режиме «только туда»
# шаги 8–11 пропускаются, номера последующих сдвигаются на −4.
_ORD = {"city_from": 1, "city_to": 2, "trip": 3,
        "out_date": 4, "out_days": 5, "out_t1": 6, "out_t2": 7,
        "ret_date": 8, "ret_days": 9, "ret_t1": 10, "ret_t2": 11,
        "cutoff": 12, "target": 13, "dig_every": 14, "dig_time": 15}

_MAX_HORIZON_DAYS = 400  # дальше расписания рейсов ещё не опубликованы

# Поля трекинга, переносимые в черновик в режиме изменения
_CLEAN_FIELDS = ("origin", "origin_name", "dest", "dest_name", "oneway",
                 "out", "ret", "cutoff", "target", "digest_every",
                 "digest_time")


# ---------------------------------------------------------------- утилиты

def _step(st) -> str:
    """Метка «N/всего» для вопроса, соответствующего st['st']."""
    n = _ORD[st["st"]]
    if (st.get("draft") or {}).get("oneway"):
        return f"{n - 4 if n > 7 else n}/11"
    return f"{n}/15"


def _d(iso) -> str:
    return f"{date.fromisoformat(iso):%d.%m.%Y}"


def _rub(n) -> str:
    return f"{n:,} ₽".replace(",", " ")


def _days_word(n: int) -> str:
    if n == 1:
        return "1 день"
    if 2 <= n <= 4:
        return f"{n} дня"
    return f"{n} дней"


def _every_word(n: int) -> str:
    if n == 1:
        return "ежедневно"
    return f"каждые {n} " + ("дня" if n < 5 else "дней")


def _win(w) -> str:
    d0, d1 = _win_dates(w)
    if int(w["days"]) == 1:
        return f"{d0:%d.%m.%Y}, {w['t1']}–{w['t2']}"
    return f"{d0:%d.%m.%Y} ({w['t1']}) – {d1:%d.%m.%Y} ({w['t2']})"


def _win_dates(w) -> tuple:
    d0 = date.fromisoformat(w["from"])
    return d0, d0 + timedelta(days=int(w["days"]) - 1)


def _trip_word(oneway) -> str:
    return "только туда" if oneway else "туда и обратно"


def _target_word(v) -> str:
    return _rub(v) if v else "не задана (уведомления по цене выключены)"


# ---------------------------------------------------------------- парсеры

def parse_date(s: str) -> date | None:
    """DD.MM.YYYY | DD.MM.YY | DD.MM. None — не удалось разобрать."""
    s = (s or "").strip().replace("-", ".")
    parts = s.split(".")
    if not (2 <= len(parts) <= 3) or not all(p.isdigit() for p in parts):
        return None
    d, m = int(parts[0]), int(parts[1])
    today = date.today()
    if len(parts) == 2:  # год не указан — ближайший подходящий
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
    """ЧЧ:ММ (диапазоны 00–23 / 00–59) -> 'ЧЧ:ММ'. None — ошибка.

    Шорткат «0» парсер не понимает сознательно: у шагов времени он
    означает 00:00/23:59, а в режиме изменения — «оставить как есть».
    """
    s = (s or "").strip()
    parts = s.split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) <= 2
                                  for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------- хранение

def _save(db, crypto, uid, state):
    db.update_user(uid, dialog_enc=crypto.enc(state) if state else None)


def _load(crypto, row) -> dict:
    return crypto.dec(row["dialog_enc"]) or {}


def _cur(st, key):
    return (st.get("draft") or {}).get(key)


def _ok(what: str, kept: bool = False) -> str:
    """Эхо-подтверждение принятого значения (перед следующим вопросом)."""
    return f"✅ Принято: {what}" + (" (оставлено прежнее)" if kept else "")


# ---------------------------------------------------------------- вопросы

def _q(st, title: str, body: str, cur=None) -> str:
    """Шапка «Шаг N/всего» + (в режиме изменения) текущее значение + тело."""
    head = f"<b>Шаг {_step(st)}: {title}</b>\n\n"
    if st.get("edit") and cur is not None:
        head += (f"Текущее значение: {cur}. Чтобы оставить его, "
                 f"введите <b>0</b>.\n\n")
    return head + body


def _q_city_from(st) -> str:
    return _q(
        st, "город вылета",
        "Введите город (или аэропорт) вылета — например «Москва» или "
        "«Шереметьево». Опечатки не страшны.\n"
        "Я найду варианты и предложу выбрать номер. Если городов с таким "
        "названием несколько — добавьте страну через запятую: "
        "«Мельбурн, Австралия».",
        _cur(st, "origin_name"))


def _q_city_to(st) -> str:
    return _q(
        st, "город прилёта",
        "Введите город (или аэропорт) прилёта — например «Горно-Алтайск» "
        "или «Пулково». Опечатки не страшны.\n"
        "Если городов-однофамильцев несколько — добавьте страну через "
        "запятую: «Мельбурн, Австралия».",
        _cur(st, "dest_name"))


def _q_trip(st) -> str:
    cur = _cur(st, "oneway")
    return _q(st, "тип поездки",
              "Введите <b>1</b> — только туда или <b>2</b> — туда и "
              "обратно.",
              _trip_word(cur) if cur is not None else None)


def _q_out_date(st) -> str:
    w = _cur(st, "out") or {}
    return _q(st, "дата начала окна вылета",
              "Формат ДД.ММ.ГГГГ (например 16.07.2027). Дата — не раньше "
              f"сегодняшней и не дальше чем через {_MAX_HORIZON_DAYS} дней "
              "(расписаний дальше ещё нет).",
              _d(w["from"]) if w.get("from") else None)


def _q_out_days(st) -> str:
    w = _cur(st, "out") or {}
    cur = None
    if w.get("days"):
        d0, d1 = _win_dates(w)
        cur = (f"{_days_word(int(w['days']))} "
               f"({_d(d0.isoformat())}–{_d(d1.isoformat())})")
    return _q(st, "длительность окна вылета",
              "Сколько дней подряд считать окном вылета от указанной "
              "ранее даты: <b>1</b>, <b>2</b>, <b>3</b> или <b>4</b>?",
              cur)


def _q_out_t1(st) -> str:
    w = _cur(st, "out") or {}
    body = ("Время вылета «не ранее, чем» в первый день окна, формат "
            "ЧЧ:ММ (например 20:00). "
            + ("0 — оставить текущее значение."
               if st.get("edit") else
               "Введите 0, если подходит время с начала суток (00:00)."))
    return _q(st, "время вылета «не ранее, чем» в первый день окна",
              body, w.get("t1"))


def _q_out_t2(st) -> str:
    w = _cur(st, "out") or {}
    body = ("Время вылета «не позднее, чем» в последний день окна, "
            "формат ЧЧ:ММ (например 23:00). "
            + ("0 — оставить текущее значение."
               if st.get("edit") else
               "Введите 0, если подходит время до конца суток (23:59)."))
    return _q(st, "время вылета «не позднее, чем» в последний день окна",
              body, w.get("t2"))


def _q_ret_date(st) -> str:
    w = _cur(st, "ret") or {}
    out = _cur(st, "out") or {}
    dep_end = ""
    if out.get("from") and out.get("days"):
        _, d1 = _win_dates(out)
        dep_end = f" Не раньше {_d(d1.isoformat())} — окончания окна вылета."
    return _q(st, "дата начала окна возврата",
              "Формат ДД.ММ.ГГГГ (например 31.07.2027)." + dep_end,
              _d(w["from"]) if w.get("from") else None)


def _q_ret_days(st) -> str:
    w = _cur(st, "ret") or {}
    cur = None
    if w.get("days"):
        d0, d1 = _win_dates(w)
        cur = (f"{_days_word(int(w['days']))} "
               f"({_d(d0.isoformat())}–{_d(d1.isoformat())})")
    return _q(st, "длительность окна возврата",
              "Сколько дней подряд считать окном возврата от указанной "
              "ранее даты: <b>1</b>, <b>2</b>, <b>3</b> или <b>4</b>?",
              cur)


def _q_ret_t1(st) -> str:
    w = _cur(st, "ret") or {}
    body = ("Время вылета обратно «не ранее, чем» в первый день окна "
            "возврата, формат ЧЧ:ММ. "
            + ("0 — оставить текущее значение."
               if st.get("edit") else
               "Введите 0, если подходит время с начала суток (00:00)."))
    return _q(st, "время вылета обратно «не ранее, чем» в первый день окна",
              body, w.get("t1"))


def _q_ret_t2(st) -> str:
    w = _cur(st, "ret") or {}
    body = ("Время вылета обратно «не позднее, чем» в последний день "
            "окна возврата, формат ЧЧ:ММ. "
            + ("0 — оставить текущее значение."
               if st.get("edit") else
               "Введите 0, если подходит время до конца суток (23:59)."))
    return _q(st,
              "время вылета обратно «не позднее, чем» в последний день окна",
              body, w.get("t2"))


def _q_cutoff(st) -> str:
    draft = st.get("draft") or {}
    out = draft.get("out") or {}
    interval = ""
    if out.get("from"):
        lo, hi = date.today(), date.fromisoformat(out["from"])
        interval = (f"Разрешённый интервал: {_d(lo.isoformat())} – "
                    f"{_d(hi.isoformat())} (от сегодняшнего дня до начала "
                    f"окна вылета).")
    body = ("До какой даты билет нужно купить в любом случае? Формат "
            "ДД.ММ.ГГГГ. Билеты туда и обратно обычно берутся одной "
            "покупкой, поэтому отсечка одна на поездку.\n" + interval
            + ("\n0 — оставить текущее значение." if st.get("edit")
               else ""))
    return _q(st, "дата отсечки", body,
              _d(_cur(st, "cutoff")) if _cur(st, "cutoff") else None)


def _q_target(st) -> str:
    if st.get("edit"):
        body = ("Введите новую целевую максимальную общую цену поездки в "
                "рублях с учётом всех перелётов: как только цена станет "
                "равна или ниже указанной, пришлю немедленное уведомление.\n"
                "<b>0</b> — оставить текущее значение; <b>выкл</b> — "
                "выключить уведомления по цене.")
    else:
        body = ("Введите целевую максимальную общую цену поездки в рублях "
                "с учётом всех перелётов: как только цена станет равна "
                "или НИЖЕ указанной, пришлю немедленное уведомление.\n"
                "Введите 0, если пока не знаете желаемой максимальной "
                "цены, — уведомления по цене будут выключены. Число "
                "можно будет изменить позднее.")
    return _q(st, "целевая цена", body,
              _target_word(_cur(st, "target"))
              if _cur(st, "target") is not None else None)


def _q_dig_every(st) -> str:
    cur = _cur(st, "digest_every")
    return _q(st, "частота дайджеста",
              "Введите частоту дайджеста (сводки информации о состоянии "
              "отслеживания в этот же чат с ботом): 1, 3, 7 или 14 дней. "
              "Срок отсчитывается от даты запуска отслеживания.",
              _every_word(cur) if cur else None)


def _q_dig_time(st) -> str:
    body = ("Введите желаемое время дайджеста по МСК в формате ЧЧ:ММ "
            "(например 09:00)."
            + ("\n0 — оставить текущее значение." if st.get("edit")
               else ""))
    return _q(st, "время дайджеста", body, _cur(st, "digest_time"))


# ---------------------------------------------------------------- запуск

def _draft_from_prev(prev: dict) -> dict:
    # oneway=False и target=0 — валидные значения, отсекаем только None
    d = {k: prev[k] for k in _CLEAN_FIELDS
         if k in prev and prev[k] is not None}
    if d.get("oneway"):
        d.pop("ret", None)
    return d


def begin(db, crypto, uid: str) -> str:
    prev_st = _load(crypto, db.get_user(uid))
    if prev_st.get("st") == "wl":
        return ("⏳ Вы в листе ожидания — ваши параметры уже сохранены. "
                "Когда появится место, пришлю сообщение (или попробуйте "
                "/retry). Изменить параметры: /cancel, затем /watch.")
    st = {"st": "city_from", "draft": {}}
    note = ""
    if prev_st.get("st") == "stopped" and prev_st.get("last"):
        st["edit"] = True
        st["last"] = prev_st["last"]
        st["draft"] = _draft_from_prev(prev_st["last"])
        note = ("Вы настраиваете отслеживание заново — прежние значения "
                "помню: покажу их в каждом вопросе, <b>0</b> — оставить.\n\n")
    _save(db, crypto, uid, st)
    return ("🧭 <b>Настройка отслеживания</b>\n\n" + note
            + _q_city_from(st) + "\n<i>Прервать: /cancel</i>")


# ---------------------------------------------------------------- сводка

def summary(t: dict) -> str:
    out = [
        "📋 <b>Проверьте параметры:</b>",
        f"1. Откуда: {t['origin_name']}",
        f"2. Куда: {t['dest_name']}",
        f"3. Тип: {_trip_word(t.get('oneway'))}",
        f"4. Окно вылета: {_win(t['out'])}",
    ]
    if not t.get("oneway") and t.get("ret"):
        out.append(f"5. Окно возврата: {_win(t['ret'])}")
    out += [
        f"6. Дата отсечки (купить обязательно): {_d(t['cutoff'])}",
        f"7. Целевая цена: {_target_word(t.get('target'))}",
        f"8. Дайджест: {_every_word(t['digest_every'])} в "
        f"{t['digest_time']} МСК",
        "",
        "Отправьте <b>1</b> — сохранить, <b>0</b> — настроить заново. "
        "<i>/cancel — выйти без сохранения.</i>",
    ]
    return "\n".join(out)

# ---------------------------------------------------------------- ввод

def feed(db, crypto, uid: str, text: str) -> list:
    row = db.get_user(uid)
    st = _load(crypto, row)
    if not st:
        return ["Отправьте /watch, чтобы настроить отслеживание."]
    st["draft"] = st.get("draft") or {}
    fn = globals().get("_h_" + st["st"])
    if not fn:  # неизвестное состояние — тихий перезапуск мастера
        _save(db, crypto, uid, None)
        return ["Отправьте /watch, чтобы настроить отслеживание."]
    try:
        replies = fn(db, crypto, uid, st, text.strip())
    except Exception as e:  # защита от каскада ошибок в диалоге
        log.exception("dialog error: %s", e)
        _save(db, crypto, uid, None)
        return ["⚠️ Ошибка диалога, мастер перезапущен. Отправьте /watch."]
    if isinstance(replies, tuple):  # (draft, "SAVE") от шага confirm
        return [replies]
    if isinstance(replies, str):    # защита от «голых» строк в хендлерах
        return [replies]
    return replies


# ---------------------------------------------------------------- города

def _h_city_from(db, crypto, uid, st, text):
    return _place_step(db, crypto, uid, st, text, "origin", "city_to")


def _h_city_to(db, crypto, uid, st, text):
    return _place_step(db, crypto, uid, st, text, "dest", "trip")


def _place_step(db, crypto, uid, st, text, field, next_st):
    """Шаг города: поиск и МЕНЮ вариантов (всегда, даже при одном).

    В режиме изменения «0» оставляет прежний город без поиска.
    """
    what = "город вылета" if field == "origin" else "город прилёта"
    if st.get("edit") and text == "0" and _cur(st, field + "_name"):
        kept = _cur(st, field + "_name")
        st["st"] = next_st
        _save(db, crypto, uid, st)
        echo = _ok(f"{what} — {kept}", kept=True)
        nxt = _q_trip(st) if next_st == "trip" else _q_city_to(st)
        return [echo + "\n\n" + nxt]
    cands, more, error = places.search(text)
    if error:
        return ["🛰 Сервис поиска городов недоступен, попробуйте позже. "
                "Или введите название ещё раз."]
    if not cands:
        return ["🤷 Не нашёл. Проверьте название и повторите ввод "
                "(город или аэропорт, можно с опечатками)."]
    st["cand"] = cands
    st["pick_for"] = field
    st["st"] = "pick"
    _save(db, crypto, uid, st)
    lines = ["Выберите вариант (ответьте номером):"]
    lines += [f"{i}. {c['label']}" for i, c in enumerate(cands, 1)]
    if len(cands) == 1:
        lines.append("Если вариант подходит — введите 1.")
    if more:  # показаны лучшие, а не отказ — тупика нет
        lines.append("\nПоказал лучшие 5 — если нужного пункта нет, "
                     "уточните: добавьте буквы или страну через запятую "
                     "(«Мельбурн, Австралия»).")
    return ["\n".join(lines)]


def _h_pick(db, crypto, uid, st, text):
    cands = st.get("cand") or []
    if not text.isdigit() or not (1 <= int(text) <= len(cands)):
        return [f"Введите номер от 1 до {len(cands)} (или /cancel)."]
    c = cands[int(text) - 1]
    field = st["pick_for"]
    st["draft"][field] = c["code"]
    st["draft"][field + "_name"] = c["label"]
    st["cand"] = None
    st["st"] = "trip" if field == "dest" else "city_to"
    _save(db, crypto, uid, st)
    what = "город вылета" if field == "origin" else "город прилёта"
    echo = _ok(f"{what} — {c['label']}")
    nxt = _q_trip(st) if st["st"] == "trip" else _q_city_to(st)
    return [echo + "\n\n" + nxt]


# ---------------------------------------------------------------- тип

def _h_trip(db, crypto, uid, st, text):
    cur = _cur(st, "oneway")
    if st.get("edit") and text == "0" and cur is not None:
        st["st"] = "out_date"
        _save(db, crypto, uid, st)
        return [_ok(f"тип поездки — {_trip_word(cur)}", kept=True)
                + "\n\n" + _q_out_date(st)]
    if text not in ("1", "2"):
        return ["Введите <b>1</b> (только туда) или <b>2</b> (туда и "
                "обратно)."]
    oneway = text == "1"
    st["draft"]["oneway"] = oneway
    if oneway:
        st["draft"].pop("ret", None)  # окна возврата быть не должно
    st["st"] = "out_date"
    _save(db, crypto, uid, st)
    if oneway:
        echo = (_ok("тип поездки — только туда. Шагов теперь 11, "
                    "нумерация пересчитана"))
    else:
        echo = _ok("тип поездки — туда и обратно (всего шагов 15)")
    return [echo + "\n\n" + _q_out_date(st)]


# ------------------------------------------------- окно вылета (4–7/15)

def _h_out_date(db, crypto, uid, st, text):
    w = st["draft"].get("out") or {}
    if st.get("edit") and text == "0" and w.get("from"):
        kept = w["from"]
        st["st"] = "out_days"
        _save(db, crypto, uid, st)
        return [_ok(f"дата начала окна вылета — {_d(kept)}", kept=True)
                + "\n\n" + _q_out_days(st)]
    d = parse_date(text)
    if not d:
        return ["Не понял дату. Формат ДД.ММ.ГГГГ, например 16.07.2027."]
    if d < date.today():
        return ["Эта дата уже прошла. Введите дату не раньше сегодняшней."]
    if d > date.today() + timedelta(days=_MAX_HORIZON_DAYS):
        return [f"Слишком далеко: расписаний на эту дату ещё нет "
                f"(максимум ~{_MAX_HORIZON_DAYS} дней вперёд)."]
    st["draft"]["out"] = {"from": d.isoformat(),
                          "days": w.get("days"),
                          "t1": w.get("t1"), "t2": w.get("t2")}
    st["st"] = "out_days"
    _save(db, crypto, uid, st)
    echo = _ok(f"дата начала окна вылета — {d:%d.%m.%Y}")
    return [echo + "\n\n" + _q_out_days(st)]


def _days_step(db, crypto, uid, st, text, wkey, next_st):
    """Длительность окна (шаг 5/15 или 9/15). Без умолчаний: 1–4."""
    w = st["draft"].get(wkey) or {}
    noun = "вылета" if wkey == "out" else "возврата"
    q_t1 = _q_out_t1 if wkey == "out" else _q_ret_t1
    if st.get("edit") and text == "0" and w.get("days"):
        kept = int(w["days"])
        st["st"] = next_st
        _save(db, crypto, uid, st)
        return [_ok(f"длительность окна {noun} — {_days_word(kept)}",
                    kept=True) + "\n\n" + q_t1(st)]
    if text not in ("1", "2", "3", "4"):
        return ["Введите количество дней: <b>1</b>, <b>2</b>, <b>3</b> "
                "или <b>4</b>."]
    w["days"] = int(text)
    st["draft"][wkey] = w
    st["st"] = next_st
    _save(db, crypto, uid, st)
    d0, d1 = _win_dates(w)
    echo = _ok(f"длительность окна {noun} — {_days_word(int(text))} "
               f"({_d(d0.isoformat())}–{_d(d1.isoformat())})")
    return [echo + "\n\n" + q_t1(st)]


def _h_out_days(db, crypto, uid, st, text):
    return _days_step(db, crypto, uid, st, text, "out", "out_t1")


def _h_ret_days(db, crypto, uid, st, text):
    return _days_step(db, crypto, uid, st, text, "ret", "ret_t1")


def _time_step(db, crypto, uid, st, text, wkey, tkey, next_st, shortcut,
               q_next, what, err_hint):
    """Шаг времени (6, 7, 10, 11). «0»: без edit — шорткат (00:00/23:59),
    в режиме изменения — оставить текущее."""
    w = st["draft"].get(wkey) or {}
    if st.get("edit") and text == "0" and w.get(tkey):
        kept = w[tkey]
        st["st"] = next_st
        _save(db, crypto, uid, st)
        return [_ok(f"{what} — {kept}", kept=True) + "\n\n" + q_next(st)]
    if text == "0":
        t = shortcut
    else:
        t = parse_time(text)
        if not t:
            return [err_hint]
    w[tkey] = t
    st["draft"][wkey] = w
    st["st"] = next_st
    _save(db, crypto, uid, st)
    return [_ok(f"{what} — {t}") + "\n\n" + q_next(st)]


def _h_out_t1(db, crypto, uid, st, text):
    return _time_step(db, crypto, uid, st, text, "out", "t1", "out_t2",
                      "00:00", _q_out_t2, "время вылета «не ранее»",
                      "Не понял время. Формат ЧЧ:ММ, например 20:00 "
                      "(или 0 — 00:00).")


def _h_out_t2(db, crypto, uid, st, text):
    bad = ("Не понял время. Формат ЧЧ:ММ, например 23:00 "
           "(или 0 — 23:59).")
    w = st["draft"].get("out") or {}
    if not st.get("edit") and text == "0":
        t = "23:59"
    elif st.get("edit") and text == "0" and w.get("t2"):
        t = w["t2"]
    else:
        t = parse_time(text)
        if not t:
            return [bad]
    if int(w.get("days") or 1) == 1 and t <= w.get("t1", "00:00"):
        return [f"Время «не позднее» должно быть больше, чем {w['t1']} "
                f"(время «не ранее» в тот же единственный день). "
                f"Повторите ввод."]
    st["draft"]["out"]["t2"] = t
    oneway = st["draft"].get("oneway")
    st["st"] = "cutoff" if oneway else "ret_date"
    _save(db, crypto, uid, st)
    echo = _ok(f"время вылета «не позднее» — {t}")
    nxt = _q_cutoff(st) if oneway else _q_ret_date(st)
    return [echo + "\n\n" + nxt]


def _h_ret_t1(db, crypto, uid, st, text):
    return _time_step(db, crypto, uid, st, text, "ret", "t1", "ret_t2",
                      "00:00", _q_ret_t2,
                      "время вылета обратно «не ранее»",
                      "Не понял время. Формат ЧЧ:ММ, например 09:00 "
                      "(или 0 — 00:00).")


def _h_ret_t2(db, crypto, uid, st, text):
    bad = ("Не понял время. Формат ЧЧ:ММ, например 23:00 "
           "(или 0 — 23:59).")
    w = st["draft"].get("ret") or {}
    if not st.get("edit") and text == "0":
        t = "23:59"
    elif st.get("edit") and text == "0" and w.get("t2"):
        t = w["t2"]
    else:
        t = parse_time(text)
        if not t:
            return [bad]
    if int(w.get("days") or 1) == 1 and t <= w.get("t1", "00:00"):
        return [f"Время «не позднее» должно быть больше, чем {w['t1']}. "
                f"Повторите ввод."]
    st["draft"]["ret"]["t2"] = t
    st["st"] = "cutoff"
    _save(db, crypto, uid, st)
    echo = _ok(f"время вылета обратно «не позднее» — {t}")
    return [echo + "\n\n" + _q_cutoff(st)]


# --------------------------------------------- возврат (8–11/15, только round)

def _h_ret_date(db, crypto, uid, st, text):
    w = st["draft"].get("ret") or {}
    if st.get("edit") and text == "0" and w.get("from"):
        kept = w["from"]
        st["st"] = "ret_days"
        _save(db, crypto, uid, st)
        return [_ok(f"дата начала окна возврата — {_d(kept)}", kept=True)
                + "\n\n" + _q_ret_days(st)]
    d = parse_date(text)
    if not d:
        return ["Не понял дату. Формат ДД.ММ.ГГГГ, например 31.07.2027."]
    out = st["draft"].get("out") or {}
    if out.get("from") and out.get("days"):
        _, dep_end = _win_dates(out)
        if d < dep_end:
            return [f"Окно возврата не может начинаться раньше окончания "
                    f"окна вылета ({_d(dep_end.isoformat())}). "
                    f"Повторите ввод."]
    st["draft"]["ret"] = {"from": d.isoformat(), "days": w.get("days"),
                          "t1": w.get("t1"), "t2": w.get("t2")}
    st["st"] = "ret_days"
    _save(db, crypto, uid, st)
    echo = _ok(f"дата начала окна возврата — {d:%d.%m.%Y}")
    return [echo + "\n\n" + _q_ret_days(st)]


# -------------------------------------- отсечка / цена / дайджест (12–15/15)

def _h_cutoff(db, crypto, uid, st, text):
    if st.get("edit") and text == "0" and _cur(st, "cutoff"):
        kept = _cur(st, "cutoff")
        st["st"] = "target"
        _save(db, crypto, uid, st)
        return [_ok(f"дата отсечки — {_d(kept)}", kept=True)
                + "\n\n" + _q_target(st)]
    d = parse_date(text)
    if not d:
        return ["Не понял дату. Формат ДД.ММ.ГГГГ, например 09.07.2027."]
    out = st["draft"].get("out") or {}
    if not out.get("from"):
        return ["Сначала укажите окно вылета. Отправьте /watch."]
    lo, hi = date.today(), date.fromisoformat(out["from"])
    if not (lo <= d <= hi):
        return [f"Дата отсечки должна быть в интервале "
                f"{_d(lo.isoformat())} – {_d(hi.isoformat())} "
                f"(от сегодняшнего дня до начала окна вылета)."]
    st["draft"]["cutoff"] = d.isoformat()
    st["st"] = "target"
    _save(db, crypto, uid, st)
    echo = _ok(f"дата отсечки — {d:%d.%m.%Y}")
    return [echo + "\n\n" + _q_target(st)]


def _h_target(db, crypto, uid, st, text):
    cur = _cur(st, "target")
    if st.get("edit") and text == "0" and cur is not None:
        st["st"] = "dig_every"
        _save(db, crypto, uid, st)
        return [_ok(f"целевая цена — {_target_word(cur)}", kept=True)
                + "\n\n" + _q_dig_every(st)]
    if (text or "").lower() in ("выкл", "0"):
        val = 0
    elif text.isdigit() and 1 <= int(text) <= 10_000_000:
        val = int(text)
    else:
        return ["Введите целое число рублей (например 25000); "
                "0 — без целевой цены."]
    st["draft"]["target"] = val
    st["st"] = "dig_every"
    _save(db, crypto, uid, st)
    echo = (_ok("целевая цена — не задана: уведомления по цене выключены")
            if val == 0 else _ok(f"целевая цена — {_rub(val)}"))
    return [echo + "\n\n" + _q_dig_every(st)]


def _h_dig_every(db, crypto, uid, st, text):
    cur = _cur(st, "digest_every")
    if st.get("edit") and text == "0" and cur:
        st["st"] = "dig_time"
        _save(db, crypto, uid, st)
        return [_ok(f"частота дайджеста — {_every_word(int(cur))}",
                    kept=True) + "\n\n" + _q_dig_time(st)]
    if text not in ("1", "3", "7", "14"):
        return ["Введите <b>1</b>, <b>3</b>, <b>7</b> или <b>14</b> "
                "(дней между дайджестами)."]
    st["draft"]["digest_every"] = int(text)
    st["st"] = "dig_time"
    _save(db, crypto, uid, st)
    echo = _ok(f"частота дайджеста — {_every_word(int(text))}")
    return [echo + "\n\n" + _q_dig_time(st)]


def _h_dig_time(db, crypto, uid, st, text):
    cur = _cur(st, "digest_time")
    if st.get("edit") and text == "0" and cur:
        kept = cur
        st["st"] = "confirm"
        _save(db, crypto, uid, st)
        return [_ok(f"время дайджеста — {kept} МСК", kept=True)
                + "\n\n" + summary(st["draft"])]
    t = parse_time(text)
    if not t:
        return ["Не понял время. Формат ЧЧ:ММ по МСК, например 09:00 "
                "(часы 00–23, минуты 00–59)."]
    st["draft"]["digest_time"] = t
    st["st"] = "confirm"
    _save(db, crypto, uid, st)
    echo = _ok(f"время дайджеста — {t} МСК")
    return [echo + "\n\n" + summary(st["draft"])]


# ---------------------------------------------------------------- финал

def _h_confirm(db, crypto, uid, st, text):
    t = (text or "").strip().lower()
    if t in ("1", "да", "yes", "y"):
        draft = st["draft"]
        _save(db, crypto, uid, None)  # диалог завершён
        return draft, "SAVE"  # сигнал наружу: bot.py вызовет finish_save
    if t in ("0", "нет", "no", "n"):
        st["st"] = "city_from"
        st["draft"] = (_draft_from_prev(st["last"])
                       if st.get("last") else {})
        _save(db, crypto, uid, st)
        return ["Хорошо, начнём заново.\n\n" + _q_city_from(st)]
    return ["Отправьте <b>1</b> (сохранить) или <b>0</b> (настроить "
            "заново)."]


def finish_save(db, crypto, uid, admin_uid_str: str, draft: dict,
                cfg_mu: dict) -> str:
    """Активация после подтверждения: 'ok' | 'waitlist'."""
    from . import users as U
    return U.save_tracking(db, crypto, uid, admin_uid_str, draft, cfg_mu)


# ------------------------------------------------- служебные состояния

def _h_wl(db, crypto, uid, st, text):
    return ["⏳ Вы в листе ожидания — активных мест сейчас нет. Ваши "
            "параметры сохранены: когда место освободится, пришлю "
            "сообщение (или попробуйте /retry). "
            "Настроить заново: /cancel → /watch."]


def _h_stopped(db, crypto, uid, st, text):
    return ["Отслеживание удалено. Настроить новое (прежние параметры "
            "подставлю, «0» — оставить): /watch. Справка: /help."]


def _h_stop_confirm(db, crypto, uid, st, text):
    from . import users as U
    t = (text or "").strip().lower()
    if t in ("да", "yes", "y"):
        prev = U.remove_tracking(db, crypto, uid)
        _save(db, crypto, uid,
              {"st": "stopped", "last": prev} if prev else None)
        if prev:
            return ["🗑 Отслеживание удалено.\n\nНастроить новое: /watch — "
                    "покажу прежние значения в каждом вопросе; "
                    "<b>0</b> — оставить значение."]
        return ["🗑 Отслеживание удалено. Настроить новое: /watch"]
    if t in ("нет", "no", "n"):
        _save(db, crypto, uid, None)
        return ["Отменено — отслеживание продолжает работать."]
    return ["Ответьте «да» или «нет»."]


def request_stop(db, crypto, uid: str) -> str:
    _save(db, crypto, uid, {"st": "stop_confirm", "draft": {}})
    return ("🚫 Удалить отслеживание полностью? "
            "Ответьте <b>да</b> или <b>нет</b>.")


def save_waitlist_draft(db, crypto, uid: str, draft: dict):
    _save(db, crypto, uid, {"st": "wl", "draft": draft})


def try_retry(db, crypto, uid: str, admin_uid_str: str,
              cfg_mu: dict) -> str:
    """Активация сохранённых параметров, когда появилось место."""
    row = db.get_user(uid)
    st = _load(crypto, row)
    if st.get("st") != "wl" or not st.get("draft"):
        return "none"
    from . import users as U
    res = U.save_tracking(db, crypto, uid, admin_uid_str, st["draft"], cfg_mu)
    if res == "ok":
        _save(db, crypto, uid, None)
    return res
