"""Персональные дайджесты пользователей (из накопленной базы, без API).

Расписание каждого пользователя: каждые N дней (1/3/7/14, отсчёт от
даты создания трекинга) в его время по МСК. Запускается ежечасным cron.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from . import users as U
from .trends import trend_verdict

log = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))


def _due(t: dict, row, now_msk: datetime) -> bool:
    try:
        created = date.fromisoformat(t.get("created") or row["joined"])
    except ValueError:
        return False
    every = max(int(t.get("digest_every", 1)), 1)
    hh, mm = map(int, (t.get("digest_time", "09:00")).split(":"))
    days = (now_msk.date() - created).days
    if days < every or days % every != 0:
        return False
    if row["last_digest"] == now_msk.date().isoformat():
        return False
    return (now_msk.hour, now_msk.minute) >= (hh, mm)


def build_text(t: dict, bests: list[dict], series_fn) -> str:
    """series_fn(depart_iso, ret_iso) -> [(дата, минимум)]."""
    lines = [f"📬 <b>Дайджест: {t.get('origin_name')} → "
             f"{t.get('dest_name')}"
             + ("" if t.get("oneway") else f" → {t.get('origin_name')}")
             + "</b>\n"]
    if not bests:
        lines.append("Пока нет данных — бот собирает цены. "
                     "Первые цифры появятся в ближайшие часы.")
    else:
        lines.append("<b>Текущие лучшие цены:</b>")
        for b in sorted(bests, key=lambda x: x["price"])[:6]:
            d = f"{b['depart_date'][8:10]}.{b['depart_date'][5:7]}"
            seg = f"{d}"
            if not t.get("oneway") and b["return_date"]:
                seg += f" → {b['return_date'][8:10]}.{b['return_date'][5:7]}"
            lines.append(f"• {seg}: <b>{b['price']:,} ₽</b> "
                         f"({b.get('airline') or '?'})"
                         .replace(",", " "))
        best = min(bests, key=lambda x: x["price"])
        lines.append("")
        lines.append("<b>Тренд по лучшей цене:</b>")
        lines.append(trend_verdict(series_fn(best["depart_date"],
                                             best["return_date"])))

    # отсечка: личная дата + эвристический совет + совет по накопленным данным
    lines.append("")
    cutoff = t.get("cutoff")
    if cutoff:
        left = (date.fromisoformat(cutoff) - date.today()).days
        if left <= 0:
            lines.append(f"🚨 Ваша дата отсечки ({_d(cutoff)}) наступила — "
                         f"если цена приемлема, покупайте не откладывая.")
        else:
            lines.append(f"📅 Ваша отсечка: {_d(cutoff)} (осталось {left} дн.)")
        try:
            out0 = date.fromisoformat(t["out"]["from"])
            heuristic = out0 - timedelta(weeks=6)
            if cutoff and date.fromisoformat(cutoff) > heuristic \
                    and heuristic > date.today():
                lines.append(f"💡 Совет: на пиковых направлениях билеты "
                             f"обычно разбирают до ~{_d(heuristic.isoformat())} "
                             f"(за 6 недель до вылета) — ваша отсечка позже, "
                             f"риск остаться без билетов выше.")
        except (KeyError, ValueError):
            pass
        hint = _cutoff_hint(t, bests, series_fn)
        if hint:
            lines.append(hint)

    c = t.get("calib")
    if c:
        lines.append("")
        lines.append(f"📊 Калибровка маршрута ({c.get('note')}):")
        lines.append(f"минимум {c.get('min')} ₽ · типичная {c.get('p50')} ₽ · "
                     f"разумная целевая цена ≈ {c.get('target_suggest')} ₽ "
                     f"(25-й перцентиль). Цель можно поменять: /watch")
    lines.append("")
    lines.append("<i>Оценки вероятностные, решение о покупке — за вами. "
                 "Команды: /status, /stop, /watch</i>")
    return "\n".join(lines)


def run_user_digests(cfg, db, crypto) -> int:
    import os
    from .notify import send_text
    tg_token = os.environ.get("TG_TOKEN")
    now_msk = datetime.now(MSK)
    sent = 0
    for row in db.all_users(status="active", with_tracking=True):
        t = crypto.dec(row["tracking_enc"])
        if not t or not _due(t, row, now_msk):
            continue
        ident = crypto.dec(row["ident_enc"]) or {}
        chat_id = ident.get("chat_id")
        bests = db.latest_best_all(row["uid"], hours=48)

        def series_fn(d, r, _uid=row["uid"]):
            return db.daily_min_series(d, r, 30, uid=_uid)

        text = build_text(t, bests, series_fn)
        if tg_token and chat_id:
            if send_text(tg_token, str(chat_id), text):
                db.update_user(row["uid"],
                               last_digest=now_msk.date().isoformat())
                sent += 1
        else:
            db.update_user(row["uid"],
                           last_digest=now_msk.date().isoformat())
    log.info("Дайджестов отправлено: %d", sent)
    return sent


def _rising_pct(series: list) -> float | None:
    """Рост цен: сравнение средних за первую и вторую половины ряда.

    series: [(YYYY-MM-DD, минимум)]. None — наблюдений меньше 10.
    """
    if not series or len(series) < 10:
        return None
    ys = [p for _, p in sorted(series)]
    h = len(ys) // 2
    first = sum(ys[:h]) / h
    second = sum(ys[h:]) / (len(ys) - h)
    if first <= 0:
        return None
    return (second / first - 1) * 100.0


def _cutoff_hint(t: dict, bests: list[dict], series_fn) -> str | None:
    """Предложение перенести дату отсечки по накопленным данным.

    Срабатывает, пока до отсечки больше 3 дней. Приоритет:
    1) калибровка есть и лучшая цена уже у минимума маршрута —
       ждать отсечки незачем;
    2) свой ряд наблюдений (≥ 10 дн.) показывает рост минимумов —
       предлагаем конкретную более раннюю дату.
    Максимум один совет за дайджест.
    """
    cutoff = t.get("cutoff")
    if not cutoff or not bests:
        return None
    try:
        cut = date.fromisoformat(cutoff)
        if (cut - date.today()).days <= 3:
            return None  # менять что-то поздно — отсечка вот-вот
        best = min(bests, key=lambda x: x["price"])
    except (KeyError, ValueError, TypeError):
        return None
    # 1) цена уже на уровне минимума маршрута (по калибровке)
    cmin = (t.get("calib") or {}).get("min")
    if cmin and best.get("price") and best["price"] <= cmin * 1.05:
        p = f"{best['price']:,}".replace(",", " ")
        m = f"{cmin:,}".replace(",", " ")
        return (f"💡 Совет по данным: лучшая цена сейчас {p} ₽ — "
                f"на уровне минимума маршрута ({m} ₽). Можно покупать, "
                f"не дожидаясь отсечки, или перенести её ближе: /watch")
    # 2) накопленный ряд наблюдений: минимумы растут
    try:
        series = series_fn(best["depart_date"], best.get("return_date"))
    except Exception:
        series = []
    pct = _rising_pct(series)
    if pct is not None and pct >= 10.0:
        prop = min(date.today() + timedelta(days=3),
                   cut - timedelta(days=1))
        return (f"💡 Совет по данным: за {len(series)} дн. наблюдений "
                f"минимальные цены на ваш маршрут выросли примерно на "
                f"{pct:.0f}%. Рекомендую перенести дату отсечки ближе — "
                f"например на {_d(prop.isoformat())}: /watch")
    return None


def _d(iso):
    return f"{date.fromisoformat(iso):%d.%m.%Y}" if iso else "—"
