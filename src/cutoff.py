"""Оценка «даты отсечки» — до какой даты билет нужно купить за любые деньги.

Эвристика для сезонных российских внутренних маршрутов (июль — пик на Алтай):
- статическая отсечка: ~6 недель до вылета цены обычно идут резко вверх,
  и растёт риск распродажи мест;
- динамическая: если тренд разворачивается вверх задолго до отсечки,
  тянуть дольше буферного срока не стоит.
Оценка вероятностная и еженедельно показывается в дайджесте.
"""
from datetime import date, timedelta


def cutoff_status(cfg: dict, today: date | None = None) -> dict:
    today = today or date.today()
    weeks = int(cfg["cutoff"].get("weeks_before_departure", 6))
    urgent = int(cfg["cutoff"].get("urgent_days_left", 14))

    earliest = min(cfg["outbound_window"]["date_from"],
                   cfg["outbound_window"]["date_to"])
    earliest = date.fromisoformat(earliest)
    static_deadline = earliest - timedelta(weeks=weeks)
    days_left = (static_deadline - today).days

    if days_left <= 0:
        return {
            "urgent": True,
            "text": (f"🚨 СРОЧНО: статическая дата отсечки "
                     f"{static_deadline:%d.%m.%Y} наступила/прошла. "
                     f"Если цена приемлема — покупайте за любые разумные деньги, "
                     f"риск не улететь растёт с каждым днём."),
        }
    if days_left <= urgent:
        return {
            "urgent": True,
            "text": (f"📅 До даты отсечки {static_deadline:%d.%m.%Y} осталось "
                     f"{days_left} дн. — дальше покупка «за любые деньги» "
                     f"становится обязательной стратегией."),
        }
    return {
        "urgent": False,
        "text": (f"📅 Дата отсечки (статистика сезонных маршрутов): "
                 f"{static_deadline:%d.%m.%Y} — до неё {days_left} дн. "
                 f"Пока есть время следить за трендом."),
    }
