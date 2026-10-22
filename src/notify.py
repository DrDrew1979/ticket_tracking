"""Telegram-уведомления: мгновенные алерты и ежедневный дайджест."""
import logging

import requests

from .cutoff import cutoff_status
from .trends import trend_verdict

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/sendMessage"


def send_text(token: str, chat_id: str, html: str) -> bool:
    try:
        r = requests.post(
            API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": html,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if not r.ok:
            log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
        return r.ok
    except requests.RequestException as e:
        log.warning("Telegram недоступен: %s", e)
        return False


def _fmt_dt_local(iso: str | None) -> str:
    if not iso:
        return "?"
    # показываем как есть: время уже местное (смещение в строке)
    return iso.replace("T", " ")[:16]


def alert_text(a: dict, cfg: dict) -> str:
    o = a["offer"]
    tr = o.get("transfers", "?")
    rtr = o.get("return_transfers", "?")
    airline = o.get("airline", "?")
    return (
        f"✈️ <b>ЛУЧШАЯ ЦЕНА ЗА 30 ДНЕЙ!</b>\n\n"
        f"💰 <b>{a['price']:,}</b> ₽ туда-обратно (цель ≤ "
        f"{cfg['price_target_rub']:,} ₽)\n"
        f"🛫 Москва → Горно-Алтайск: {_fmt_dt_local(o.get('departure_at'))}\n"
        f"🛬 Горно-Алтайск → Москва: {_fmt_dt_local(o.get('return_at'))}\n"
        f"🔁 Пересадки: {tr} туда / {rtr} обратно\n"
        f"🛩 Авиакомпания: {airline}\n\n"
        f"👉 <a href=\"{a['link']}\">Проверить и купить на Aviasales</a>\n"
        f"<i>Цена из кэша поиска — на кассе может немного отличаться. "
        f"Покупка вручную, агент оплату не проводит.</i>"
    )


def digest_text(cfg: dict, pairs_now: list[dict], best_pair_series) -> str:
    """pairs_now: [{depart, ret, price, airline, link, offer}], может быть пуст."""
    lines = ["📬 <b>Ежедневный дайджест MOW↔OGK</b>\n"]

    if pairs_now:
        lines.append("<b>Текущие лучшие цены (туда-обратно):</b>")
        for p in sorted(pairs_now, key=lambda x: x["price"])[:6]:
            lines.append(
                f"• {p['depart']:%d.%m} → {p['ret']:%d.%m}: "
                f"<b>{p['price']:,} ₽</b> ({p.get('airline', '?')})")
        best = min(pairs_now, key=lambda x: x["price"])
        lines.append("")
        lines.append("<b>Тренд по лучшей цене:</b>")
        lines.append(trend_verdict(best_pair_series))
    else:
        lines.append("Сегодня предложений в окне дат не найдено "
                     "(или API вернул пустой ответ).")

    lines.append("")
    lines.append(cutoff_status(cfg)["text"])
    lines.append("")
    lines.append("<i>Рекомендации вероятностные, решение о покупке — за вами.</i>")
    return "\n".join(lines)
