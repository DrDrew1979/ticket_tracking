"""Inline-клавиатуры визарда: календарь и «цифровые часы».

Все callback_data имеют вид "d|<...>" и укладываются в лимит Telegram
64 байта. Клавиатуры — чистые функции без состояния: состояние визарда
живёт в dialog.py (шифруется в prices.db).

Календарь: понедельник — первый день недели; суббота/воскресенье и
нерабочие праздничные дни (производственный календарь РФ, src/holidays.py)
помечаются 🔴; листание по месяцам и годам стрелками (до nav_hi — чуть
дальше горизонта, чтобы «закрытые» даты были видны); дни вне интервала
[lo..hi] не выбираются: прошлые показываются «·», а даты за горизонтом
400 дней — «✖» и нажимаются с пояснением (toast рисует dialog.py).

Часы: ДВА поля по две цифры — [ЧЧ]:[ММ] (как табло цифровых часов).
Поле заменяет цифры заново при переполнении (затирание), после двух
цифр ввод переходит к минутам, тап по полю переключает его. Состояние:
{"h": "09", "m": "37", "f": "h"|"m"} в ui диалога. Текстом можно ввести
«1057» или «10:57» целиком — парсит dialog.parse_time_text(); одна-две
цифры текстом перезаписывают активное поле.
"""
from datetime import date, timedelta

from . import holidays as H

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
CB = "d|cal|{scope}|{arg}"          # дата/навигация календаря
CLK = "d|clk|{scope}|{tok}"         # кнопки часов
NOOP = "d|noop"


def _clamp_month(y: int, m: int, lo: date, nav_hi: date) -> tuple[int, int]:
    lo_y, lo_m = lo.year, lo.month
    hi_y, hi_m = nav_hi.year, nav_hi.month
    if (y, m) < (lo_y, lo_m):
        return lo_y, lo_m
    if (y, m) > (hi_y, hi_m):
        return hi_y, hi_m
    return y, m


def month_kb(scope: str, y: int, m: int, lo: date, hi: date,
             selected: str | None = None, nav_hi: date | None = None) -> dict:
    """Клавиатура одного месяца. scope: out | ret | cut.

    Навигация обрезана по [lo..nav_hi] (nav_hi по умолчанию = hi), но
    дни валидны только в [lo..hi]: даты позже hi показываются «✖» и
    нажимаются с пояснением, дни раньше lo — «·» (неактивны).
    """
    nav_hi = nav_hi or hi
    y, m = _clamp_month(y, m, lo, nav_hi)
    cur = date(y, m, 1)
    rows: list[list[dict]] = []

    prev_m = cur - timedelta(days=1)
    next_m = (cur + timedelta(days=32)).replace(day=1)
    year_prev = cur.replace(year=cur.year - 1)
    year_next = cur.replace(year=cur.year + 1)

    def year_row():
        btns = []
        # год показываем, только если он пересекается с [lo..nav_hi]
        if year_prev.replace(month=12, day=31) >= lo:
            btns.append({"text": f"‹‹ {year_prev.year}",
                         "callback_data": CB.format(
                             scope=scope, arg=year_prev.strftime("%Y-%m"))})
        if year_next.replace(month=1, day=1) <= nav_hi:
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
    if next_m <= nav_hi:
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
        if d < lo:
            week.append({"text": "·", "callback_data": NOOP})
            continue
        if selected and d.isoformat() == selected:
            week.append({"text": f"✅{day}",
                         "callback_data": CB.format(
                             scope=scope, arg=d.isoformat())})
        elif d > hi:
            # за горизонтом (400 дней) — видна, но не выбирается
            week.append({"text": f"✖{day}",
                         "callback_data": CB.format(
                             scope=scope, arg=d.isoformat())})
        else:
            mark = "🔴" if H.is_nonworking(d) else ""
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


def clock_kb(scope: str, c: dict | None = None) -> dict:
    """Цифровые часы: экран-поля [ЧЧ]:[ММ], тап по полю переключает его."""
    c = c or {"h": "", "m": "", "f": "h"}
    tok = lambda t: CLK.format(scope=scope, tok=t)  # noqa: E731

    def field(f: str) -> str:
        v = (c.get(f) or "").ljust(2, "_")[:2]
        return f"▶ {v} ◂" if c.get("f") == f else f"[ {v} ]"

    rows = [[{"text": field("h"), "callback_data": tok("fh")},
             {"text": ":", "callback_data": NOOP},
             {"text": field("m"), "callback_data": tok("fm")}],
            [{"text": str(n), "callback_data": tok(n)} for n in (1, 2, 3)],
            [{"text": str(n), "callback_data": tok(n)} for n in (4, 5, 6)],
            [{"text": str(n), "callback_data": tok(n)} for n in (7, 8, 9)],
            [{"text": "⌫", "callback_data": tok("del")},
             {"text": "0", "callback_data": tok("0")},
             {"text": "✅ Готово", "callback_data": tok("ok")}]]
    return {"inline_keyboard": rows}


def clock_face(c: dict) -> str:
    """Экран часов текстом: {'h':'09','m':'37'} -> «[09]:[37]».

    Незаполненная позиция — «_»: {'h':'9'} -> «[9_]:[__]».
    """
    h = (c.get("h") or "").ljust(2, "_")[:2]
    m = (c.get("m") or "").ljust(2, "_")[:2]
    return f"<code>[{h}]:[{m}]</code>"


def cb_days(scope: str, n: int) -> str:
    return f"d|days|{scope}|{n}"
