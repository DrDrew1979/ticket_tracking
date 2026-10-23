"""Калибровка нового трекинга: оценка типичных цен маршрута.

ЧЕСТНОЕ ОГРАНИЧЕНИЕ: Travelpayouts Data API не отдаёт истинную историю
цен за 3 года (только текущий кэш и прогнозные минимумы по будущим датам).
Поэтому «историческая» оценка делается так:
  1) grouped_prices / month-matrix — минимальные цены по датам месяца
     вылета (и соседних месяцев), т.е. сезонный срез вперёд;
  2) со временем к этому добавляется НАША собственная накопленная
     история в prices.db (чем дольше бот работает, тем точнее).
Результат используется дайджестом для советов по целевой цене и отсечке.
"""
import logging
import statistics
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)
GP = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
MM = "https://api.travelpayouts.com/aviasales/v2/prices/month-matrix"


def _fetch(url, token, params):
    try:
        params = {**params, "token": token, "currency": "rub"}
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json().get("data") or []
        return data if isinstance(data, list) else list(data.values())
    except Exception as e:
        log.warning("calib fetch %s: %s", url, e)
        return []


def _extract_points(items) -> dict:
    """{дата: минимальная цена} из любого формата ответа."""
    pts = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        d = it.get("depart_date") or it.get("date") or it.get("departure_at")
        p = it.get("value") if it.get("value") is not None \
            else it.get("price")
        try:
            d = str(d)[:10]
            p = int(p)
            date.fromisoformat(d)
        except (ValueError, TypeError):
            continue
        if p > 0 and (d not in pts or p < pts[d]):
            pts[d] = p
    return pts


def calibrate(client, origin: str, dest: str, out_from: str,
              currency: str = "rub") -> dict | None:
    """2–3 запроса: месяц вылета + соседние (если месяц пуст)."""
    token = client.token
    month = out_from[:7]
    pts = _extract_points(_fetch(GP, token, {
        "origin": origin, "destination": dest, "group_by": "departure_date",
        "departure_at": month, "sorting": "price"}))
    if len(pts) < 5:  # месяц далеко вперёд — пробуем матрицу месяцев
        base = date.fromisoformat(out_from)
        for delta in (0, 1, -1):
            mm = (base.replace(day=1) +
                  timedelta(days=32 * delta)).strftime("%Y-%m")
            pts.update(_extract_points(_fetch(MM, token, {
                "origin": origin, "destination": dest, "month": mm})))
    if len(pts) < 5:
        return None
    vals = sorted(pts.values())
    n = len(vals)
    p25 = vals[max(0, int(n * 0.25) - 1)]
    p50 = vals[n // 2]
    target_suggest = max(1, round(p25 / 500.0) * 500)
    return {
        "min": vals[0], "p25": p25, "p50": p50, "n": n,
        "target_suggest": target_suggest,
        "note": (f"по кэшу на {n} дат месяца(ев) вокруг вашего окна: "
                 f"минимум {vals[0]} ₽, типичная цена {p50} ₽"),
        "calls": 2,
    }
