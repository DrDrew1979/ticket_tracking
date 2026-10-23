"""Трендовый анализ: падение/рост цены и рекомендация «ждать или брать».

Чистая статистика без зависимостей: линейная регрессия по ряду дневных
минимумов. Рекомендации вероятностные — в тексте это честно отражено.
"""
import math


def _linreg(xs, ys):
    """Простая линейная регрессия -> (slope, r2)."""
    n = len(xs)
    if n < 3:
        return 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    return slope, r2


def _pct_change(a, b):
    if not a:
        return 0.0
    return (a - b) / a * 100.0


def trend_verdict(series: list[tuple[str, int]]) -> str:
    """series: [(YYYY-MM-DD, дневной минимум)] по возрастанию даты.

    Возвращает человеческий вердикт на русском.
    """
    if len(series) < 10:
        return (f"📊 Данных пока мало ({len(series)} дн. из ~30). "
                f"Тренд определю после 10 дней наблюдений.")

    ys = [p for _, p in series]
    n = len(ys)

    # окно 7 дней
    xs7 = list(range(min(7, n)))
    ys7 = ys[-len(xs7):]
    slope7, r2 = _linreg(xs7, ys7)
    conf = "уверенный" if r2 > 0.5 else "слабый"

    last = ys[-1]
    min30 = min(ys)
    days_since_min = n - 1 - ys.index(min30)

    if slope7 < -1.0 and days_since_min >= 2:
        drop3 = _pct_change(ys[-4], last) if n >= 4 else _pct_change(ys[0], last)
        return (f"📉 Цена падает ({conf} тренд): −{drop3:.1f}% за последние "
                f"3–4 дня. Статистически имеет смысл подождать ещё ~3–7 дней — "
                f"продолжаю следить, пуш пришлю при новом минимуме.")
    if slope7 > 1.0 and last > min30 * 1.03 and days_since_min <= 5:
        rise = _pct_change(min30, last)
        return (f"📈 Цена начала расти с минимума {min30} ₽ (уже +{rise:.1f}%, "
                f"{conf} тренд). Ждать дальше нерационально — рекомендую брать "
                f"билет по текущей цене {last} ₽.")
    return (f"➖ Цена стабильна: {last} ₽ "
            f"(минимум за период {min30} ₽). Наблюдаю дальше.")
