"""Окна дат/времени, фильтры и главное ценовое правило (per-user).

Семантика временного окна (по ТЗ):
  - время «не ранее» действует ТОЛЬКО на первый день окна;
  - время «не позднее» — ТОЛЬКО на последний день;
  - средние дни окна — любое время суток.
Один день: оба ограничения действуют одновременно.
"""
import logging
from datetime import date, datetime, time as dtime, timedelta

from .fetcher import build_search_url

log = logging.getLogger(__name__)


def win_dates(w: dict) -> list[date]:
    d0 = date.fromisoformat(w["from"])
    return [d0 + timedelta(days=i) for i in range(int(w["days"]))]


def in_window_time(dt: datetime, w: dict) -> bool:
    if dt is None or w is None:
        return False
    d0 = date.fromisoformat(w["from"])
    d1 = d0 + timedelta(days=int(w["days"]) - 1)
    if not (d0 <= dt.date() <= d1):
        return False
    t1 = dtime.fromisoformat(w["t1"])
    t2 = dtime.fromisoformat(w["t2"])
    if int(w["days"]) == 1:
        return t1 <= dt.time() <= t2
    if dt.date() == d0 and dt.time() < t1:
        return False
    if dt.date() == d1 and dt.time() > t2:
        return False
    return True


def _parse_iso(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def passes_filters(offer: dict, out_w: dict, ret_w: dict | None) -> bool:
    dep = _parse_iso(offer.get("departure_at"))
    if not in_window_time(dep, out_w):
        return False
    if ret_w is not None:
        ret = _parse_iso(offer.get("return_at"))
        if not in_window_time(ret, ret_w):
            return False
    return True


def owner_windows(cfg: dict) -> tuple[dict, dict]:
    """Окна владельца из config.yaml -> единый формат {from, days, t1, t2}."""
    def conv(w):
        from .config import _dates_between  # локальный импорт без цикла
        days = len(_dates_between(date.fromisoformat(w["date_from"]),
                                  date.fromisoformat(w["date_to"])))
        return {"from": w["date_from"], "days": days,
                "t1": w["time_from"], "t2": w["time_to"]}
    return conv(cfg["outbound_window"]), conv(cfg["return_window"])


def tracking_pairs(t: dict) -> list[tuple[date, date | None]]:
    """Все пары (дата туда, дата обратно|None) трекинга."""
    out = win_dates(t["out"])
    if t.get("oneway") or not t.get("ret"):
        return [(d, None) for d in out]
    return [(d, r) for d in out for r in win_dates(t["ret"])]


def evaluate_pair(db, client, uid: str, *, origin, dest, d: date, r,
                  oneway: bool, out_w: dict, ret_w: dict | None,
                  target: int, currency: str = "rub",
                  marker: str | None = None, carry_on: bool = False,
                  win_days: int = 30, warmup_days: int = 2,
                  dedup_hours: int = 12, improve_pct: float = 0.5) -> list[dict]:
    """Опрос одной пары: запрос -> change-only запись -> правило min30.

    Правило: best_price <= target И best_price <= минимум за win_days.
    r=None — one-way (без обратного плеча).
    """
    offers = client.fetch_roundtrip(
        origin, dest, d.isoformat(), r.isoformat() if r else None, currency)
    if not offers:
        return []

    good = [o for o in offers if passes_filters(o, out_w, ret_w)]
    if not good:
        return []

    best = min(good, key=lambda o: int(o["price"]))
    best_price = int(best["price"])
    r_iso = r.isoformat() if r else ""

    db.store_best(uid, d.isoformat(), r_iso, best)

    alerts = []
    warmed = db.distinct_days(d.isoformat(), r_iso, 60,
                              uid=uid) >= warmup_days + 1
    hist_min = db.history_min(d.isoformat(), r_iso, win_days, uid=uid)
    if warmed and hist_min is not None \
            and best_price <= hist_min and best_price <= target:
        last = db.recent_alert("min30", d.isoformat(), r_iso, dedup_hours,
                               uid=uid)
        resend = last is None or best_price <= int(last["price"]) * \
            (1 - improve_pct / 100.0)
        if resend:
            alerts.append({
                "kind": "min30", "uid": uid,
                "depart": d, "ret": r, "price": best_price,
                "offer": best, "hist_min": hist_min,
                "link": build_search_url(best.get("link", ""), marker,
                                         carry_on),
            })
            db.record_alert("min30", d.isoformat(), r_iso, best_price,
                            best.get("link", ""), uid=uid)
    return alerts
