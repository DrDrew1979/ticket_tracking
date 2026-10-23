"""Отправка сообщений в Telegram (единый примитив send_text).

Все тексты (алерты, дайджесты, сервисные оповещения) собирают свои
модули (monitor, digest, main) и отправляют через send_text.
"""
import logging

import requests

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
