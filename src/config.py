"""Загрузка конфигурации и вспомогательные функции для окон дат."""
import yaml
from datetime import date, timedelta
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path=None):
    with open(path or DEFAULT_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _dates_between(d1: date, d2: date):
    """Все даты включительно между d1 и d2."""
    out, cur = [], d1
    while cur <= d2:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def outbound_dates(cfg):
    w = cfg["outbound_window"]
    return _dates_between(date.fromisoformat(w["date_from"]),
                          date.fromisoformat(w["date_to"]))


def return_dates(cfg):
    w = cfg["return_window"]
    return _dates_between(date.fromisoformat(w["date_from"]),
                          date.fromisoformat(w["date_to"]))


def date_pairs(cfg):
    """Все комбинации (дата вылета туда, дата вылета обратно)."""
    return [(d, r) for d in outbound_dates(cfg) for r in return_dates(cfg)]


def stop_date(cfg):
    """Дата остановки мониторинга.

    mode: auto    -> последняя дата return_window + grace_days
                     (следует за сменой дат в конфиге автоматически);
    mode: manual  -> фиксированная stop_date из конфига.
    """
    s = cfg.get("stop") or {}
    mode = s.get("mode", "auto")
    if mode == "manual" and s.get("stop_date"):
        return date.fromisoformat(s["stop_date"])
    last_ret = max(date.fromisoformat(cfg["return_window"]["date_from"]),
                   date.fromisoformat(cfg["return_window"]["date_to"]))
    return last_ret + timedelta(days=int(s.get("grace_days", 3)))
