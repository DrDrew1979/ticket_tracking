"""Производственный календарь РФ: нерабочие и праздничные дни.

Источники:
- data/holidays.json — обновляется раз в ~90 дней задачей holidays-update
  (workflow) из открытого API isdayoff.ru (официальный производственный
  календарь, включая переносы выходных);
- встроенный статический фолбэк: фиксированные нерабочие праздничные даты
  (1–8 января, 23 февраля, 8 марта, 1 мая, 9 мая, 12 июня, 4 ноября) на
  текущий и два следующих года — используется, если файла нет или API
  недоступен. Переносы выходных в фолбэке не учтены — их добавит
  плановое обновление.

Формат holidays.json:
{
  "updated": "2026-10-23",
  "source": "isdayoff.ru",
  "nonworking": ["2027-01-01", ...],      // все нерабочие праздничные
                                           // и перенесённые выходные
  "working_weekends": ["2027-02-13", ...] // официально РАБОЧИЕ субботы/вс
}
"""
import json
import logging
import os
import urllib.request
from datetime import date, timedelta

log = logging.getLogger(__name__)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(os.path.dirname(HERE), "data", "holidays.json")

API = "https://isdayoff.ru/api/getdata"

# Фиксированные нерабочие праздничные дни РФ (ст. 112 ТК РФ),
# без учёта переносов (переносы приносит обновление с isdayoff.ru)
FIXED_HOLIDAYS = ((1, 1), (1, 2), (1, 3), (1, 4), (1, 5), (1, 6), (1, 7),
                  (1, 8), (2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4))

_cache: dict | None = None


def _static_data() -> dict:
    """Фолбэк без файла и без сети: фиксированные праздники на 3 года."""
    y0 = date.today().year
    nonworking = []
    for y in (y0, y0 + 1, y0 + 2):
        for m, d in FIXED_HOLIDAYS:
            try:
                nonworking.append(date(y, m, d).isoformat())
            except ValueError:
                pass
    return {"updated": date.today().isoformat(), "source": "static-fallback",
            "nonworking": sorted(set(nonworking)), "working_weekends": []}


def load_data(force: bool = False) -> dict:
    """Читает holidays.json; при его отсутствии — статический фолбэк."""
    global _cache
    if _cache is not None and not force:
        return _cache
    data = None
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("nonworking"), list):
            data = None
    except (OSError, ValueError):
        data = None
    if data is None:
        data = _static_data()
        try:  # сразу создать файл, чтобы обновление начиналось с базы
            os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
            with open(DATA_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
    data["nw"] = set(data.get("nonworking") or [])
    data["ww"] = set(data.get("working_weekends") or [])
    _cache = data
    return data


def is_nonworking(d: date) -> bool:
    """Нерабочий день: выходной (сб/вс, кроме официально рабочих) или
    праздник/перенесённый выходной по производственному календарю."""
    data = load_data()
    iso = d.isoformat()
    if iso in data["nw"]:
        return True
    if d.weekday() >= 5:  # суббота/воскресенье
        return iso not in data["ww"]
    return False


def is_holiday(d: date) -> bool:
    """Праздничный/нерабочий по календарю день (в т.ч. перенесённый
    выходной на будень) — без обычных суббот и воскресений."""
    return d.isoformat() in load_data()["nw"]


def refresh(path: str | None = None, years: int = 3,
            y0: int | None = None) -> dict:
    """Обновляет holidays.json из isdayoff.ru (годы: y0 + N-1).

    Возвращает записанные данные. Бросает исключение, если ни один год
    не удалось получить — тогда старый файл остаётся нетронутым.
    """
    path = path or DATA_PATH
    y0 = y0 or date.today().year
    nonworking, working_weekends = set(), set()
    got = []
    for y in range(y0, y0 + years):
        codes = _fetch_year(y)
        if codes:
            got.append(y)
            _absorb(y, codes, nonworking, working_weekends)
    if not got:
        raise RuntimeError("isdayoff.ru недоступен — holidays.json не тронут")
    data = {"updated": date.today().isoformat(), "source": "isdayoff.ru",
            "nonworking": sorted(nonworking),
            "working_weekends": sorted(working_weekends)}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    global _cache
    _cache = None
    load_data(force=True)
    log.info("Производственный календарь обновлён: годы %s, нерабочих %d, "
             "рабочих выходных %d", got, len(nonworking), len(working_weekends))
    return data


def _fetch_year(year: int) -> str | None:
    """Коды дней года от isdayoff.ru: 0 — рабочий, 1 — нерабочий,
    2 — сокращённый, 4 — рабочий выходной. None — ошибка."""
    url = f"{API}?year={year}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                s = r.read().decode("utf-8", "replace").strip()
            s = s.replace("\n", "").replace(",", "")
            if s.isdigit() and len(s) in (365, 366):
                return s
        except Exception as e:  # noqa: BLE001
            log.warning("isdayoff %d (попытка %d): %s", year, attempt + 1, e)
    return None


def _absorb(year: int, codes: str, nonworking: set, working_weekends: set):
    d = date(year, 1, 1)
    for ch in codes:
        iso = d.isoformat()
        if ch == "1":
            nonworking.add(iso)
        elif ch == "4":
            working_weekends.add(iso)
        d += timedelta(days=1)
