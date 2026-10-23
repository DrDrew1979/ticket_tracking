"""Пинг внешнего «маячка» живости (healthchecks.io и совместимые).

Протокол простой: GET на URL = «успешный запуск», GET на URL + "/fail"
= «запуск упал». Если пинги пропадают дольше периода+grace — сервис
сам пришлёт вам уведомление (email/Telegram и т.п.).
"""
import logging

import requests

log = logging.getLogger(__name__)


def ping(url: str, ok: bool = True, timeout: int = 10) -> bool:
    if not url:
        return False
    target = url if ok else url.rstrip("/") + "/fail"
    try:
        requests.get(target, timeout=timeout)
        return True
    except requests.RequestException as e:
        log.warning("Healthcheck недоступен: %s", e)
        return False
