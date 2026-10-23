"""Inline-клавиатуры визарда: календарь и «цифровые часы».

Все callback_data имеют вид "d|<...>" и укладываются в лимит Telegram
64 байта. Клавиатуры — чистые функции без состояния: состояние визарда
живёт в dialog.py (шифруется в prices.db).

Календарь: понедельник — первый день недели; суббота/воскресенье и
нерабочие праздничные дни (производственный календарь РФ, src/holidays.py)
помечаются 🔴; листание по месяцам и годам стрелками; дни вне допустимого
интервала не нажимаются («·»).

Часы: буфер из 4 цифр в состоянии диалога; экран показывает ЧЧ:ММ с
незаполненными позициями «_»; кнопки 0–9, ⌫ и «Готово». Текстом можно
ввести «1057» или «10:57» — парсит dialog.parse_time_text().
"""
from datetime import date, timedelta

from . import holidays as H

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
CB = "d|cal|{scope}|{arg}"          # дата/навигация календаря
CLK = "d|clk|{scope}|{tok}"         # кнопки часов
NOOP = "d|noop"


def _clamp_month(y: int, m: int, lo: date, hi: date) -> tuple[int, int]:
    lo_y, lo_m = lo.year, lo.month
    hi_y, hi_m = hi.year, hi.month
    if (y, m) < (lo_y, lo_m):
        return lo_y, lo_m
    if (y, m) > (hi_y, hi_m):
        return hi_y, hi_m
    return y, m


def month_kb(scope: str, y: int, m: int, lo: date, hi: date,
             selected: str | None = None) -> dict:
    """Клавиатура одного месяца. scope: out | ret | cut.

    Навигация: «‹‹ год ››» и «‹ месяц ›», обрезаны по [lo..hi].
    """
    y, m = _clamp_month(y, m, lo, hi)
    cur = date(y, m, 1)
    rows: list[list[dict]] = []

    prev_m = cur - timedelta(days=1)
    next_m = (cur + timedelta(days=32)).replace(day=1)
    year_prev = cur.replace(year=cur.year - 1)
    year_next = cur.replace(year=cur.year + 1)

    def year_row():
        btns = []
        # год показываем, только если он пересекается с [lo..hi]
        if year_prev.replace(month=12, day=31) >= lo:
            btns.append({"text": f"‹‹ {year_prev.year}",
                         "callback_data": CB.format(
                             scope=scope, arg=year_prev.strftime("%Y-%m"))})
        if year_next.replace(month=1, day=1) <= hi:
            btns.append({"text": f"{year_next.year} ››",
                         "callback_data": CB.format(
                             scope=scope, arg=year_next.strftime("%Y-%m"))})
        return btns

    yr = year_row()
    if yr:
        rows.append(yr)

    mrow = []
    if prev_m >= date(prev_m.year, lo.month, 1):
        mrow.append({"text": "‹",
                     "callback_data": CB.format(
                         scope=scope, arg=prev_m.strftime("%Y-%m"))})
    mrow.append({"text": f"{MONTHS[m - 1].capitalize()} {y}",
                 "callback_data": NOOP})
    if next_m <= hi:
        mrow.append({"text": "›",
                     "callback_data": CB.format(
                         scope=scope, arg=next_m.strftime("%Y-%m"))})
    rows.append(mrow)
    rows.append([{"text": d, "callback_data": NOOP} for d in DOW])

    first_dow = date(y, m, 1).weekday()  # Пн=0
    ndays = (date(y + (m == 12), (m % 12) + 1, 1) - cur).days
    week = []
    for _ in range(first_dow):
        week.append({"text": "·", "callback_data": NOOP})
    for day in range(1, ndays + 1):
        d = date(y, m, day)
        if d < lo or d > hi:
            week.append({"text": "·", "callback_data": NOOP})
            continue
        mark = "🔴" if H.is_nonworking(d) else ""
        if selected and d.isoformat() == selected:
            mark = "✅"
        week.append({"text": f"{mark}{day}",
                     "callback_data": CB.format(
                         scope=scope, arg=d.isoformat())})
        if len(week) == 7:
            rows.append(week)
            week = []
    if week:
        week += [{"text": "·", "callback_data": NOOP}
                 for _ in range(7 - len(week))]
        rows.append(week)
    return {"inline_keyboard": rows}


def clock_kb(scope: str) -> dict:
    """Цифровые часы: буфер живёт в состоянии диалога, экран — в тексте."""
    tok = lambda t: CLK.format(scope=scope, tok=t)  # noqa: E731
    rows = [[{"text": str(n), "callback_data": tok(n)} for n in (1, 2, 3)],
            [{"text": str(n), "callback_data": tok(n)} for n in (4, 5, 6)],
            [{"text": str(n), "callback_data": tok(n)} for n in (7, 8, 9)],
            [{"text": "⌫", "callback_data": tok("del")},
             {"text": "0", "callback_data": tok("0")},
             {"text": "✅ Готово", "callback_data": tok("ok")}]]
    return {"inline_keyboard": rows}


def clock_face(buf: str) -> str:
    """Экран часов: '10' -> «10 : __», '1057' -> «10 : 57»."""
    buf = (buf or "")[:4]
    hh = buf[:2].ljust(2, "_")
    mm = buf[2:].ljust(2, "_")
    return f"<code>{hh} : {mm}</code>"


def cb_days(scope: str, n: int) -> str:
    return f"d|days|{scope}|{n}"
