"""Фильтры предложений и главное ценовое правило."""
import logging
from datetime import datetime, time as dtime

from .fetcher import build_search_url

log = logging.getLogger(__name__)


def _parse_iso(ts: str):
    """ISO-время из API ('2027-07-16T21:35:00+03:00') -> datetime.

    Если смещение отсутствует, считаем время уже местным (аэропортовым).
    """
    if not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt


def _in_time_window(dt: datetime, window: dict) -> bool:
    """Проверка местного времени вылета против окна из конфига."""
    if dt is None:
        return False
    t_from = dtime.fromisoformat(window["time_from"])
    t_to = dtime.fromisoformat(window["time_to"])
    return t_from <= dt.time() <= t_to


def _passes_filters(offer: dict, cfg: dict,
                    out_w: dict, ret_w: dict) -> bool:
    dep = _parse_iso(offer.get("departure_at"))
    ret = _parse_iso(offer.get("return_at"))
    if not _in_time_window(dep, out_w):
        return False
    if not _in_time_window(ret, ret_w):
        return False

    max_tr = cfg.get("max_transfers")
    if max_tr is not None:
        tr = offer.get("transfers")
        rtr = offer.get("return_transfers")
        if tr is None or rtr is None:
            return False  # нет данных о пересадках — отбрасываем
        if int(tr) > int(max_tr) or int(rtr) > int(max_tr):
            return False

    max_dur = cfg.get("max_duration_hours")
    if max_dur is not None:
        d1 = offer.get("duration_to") or 0      # минуты
        d2 = offer.get("duration_back") or 0
        if max(d1, d2) > float(max_dur) * 60:
            return False
    return True


def evaluate_pair(cfg: dict, db, client, depart, ret) -> list[dict]:
    """Запрашивает цены пары дат, кладёт в базу и возвращает сработавшие алерты."""
    route = cfg["route"]
    offers = client.fetch_roundtrip(
        route["origin"], route["destination"],
        depart.isoformat(), ret.isoformat(), cfg.get("currency", "rub"),
    )
    if offers:
        db.store_offers(depart.isoformat(), ret.isoformat(), offers)

    good = [o for o in offers if _passes_filters(o, cfg,
                                                 cfg["outbound_window"],
                                                 cfg["return_window"])]
    if not good:
        return []

    best = min(good, key=lambda o: int(o["price"]))
    best_price = int(best["price"])
    pair = (depart.isoformat(), ret.isoformat())
    alerts = []

    # --- главное правило: цена = минимум за N дней И <= целевой ---
    m = cfg["monitor"]
    win_days = int(m.get("history_days_for_min", 30))
    target = int(cfg["price_target_rub"])
    warmup = int(m.get("warmup_days", 2))
    dedup_h = int(m.get("alert_dedup_hours", 12))

    warmed_up = db.distinct_days(*pair) >= warmup + 1
    hist_min = db.history_min(*pair, win_days)

    if warmed_up and hist_min is not None \
            and best_price <= hist_min and best_price <= target:
        last = db.recent_alert("min30", *pair, dedup_h)
        resend = False
        if last is None:
            resend = True
        else:
            improve = float(m.get("alert_improvement_pct", 0.5)) / 100.0
            if best_price <= int(last["price"]) * (1 - improve):
                resend = True
        if resend:
            alerts.append({
                "kind": "min30",
                "depart": depart, "ret": ret,
                "price": best_price,
                "offer": best,
                "hist_min": hist_min,
                "link": build_search_url(
                    best.get("link", ""), cfg.get("marker") or None,
                    bool(cfg.get("carry_on_only")),
                ),
            })
            db.record_alert("min30", *pair, best_price, best.get("link", ""))
    return alerts
