"""Клиент Travelpayouts Data API (кэш поиска Aviasales).

Документация: https://support.travelpayouts.com/hc/ru/categories/200358578
Метод: aviasales/v3/prices_for_dates — цены туда-обратно по датам из кэша.
Ответ содержит: price, airline, departure_at/return_at (ISO с местным
смещением), transfers / return_transfers, duration_to/duration_back (мин),
link (готовый путь поиска для deeplink).

Важно: цены кэшированные, финальная цена подтверждается на кассе Aviasales.
"""
import logging
import time

import requests

log = logging.getLogger(__name__)

V3_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


class TPClient:
    def __init__(self, token: str, marker: str | None = None, timeout: int = 20):
        self.token = token
        self.marker = marker or None
        self.timeout = timeout

    def fetch_roundtrip(self, origin: str, destination: str,
                        depart_date: str, return_date: str | None = None,
                        currency: str = "rub", limit: int = 15) -> list[dict]:
        """Список предложений (кэш) по паре дат; return_date=None — one-way.

        depart_date/return_date — YYYY-MM-DD или YYYY-MM (месяц: кэш по
        всем датам месяца, используется как фолбэк, если по точным датам
        в кэше пусто — дальние даты наполняются по мере спроса).
        """
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": depart_date,   # YYYY-MM-DD или YYYY-MM
            "direct": "false",             # прямые + с пересадками
            "sorting": "price",
            "order_by": "price",
            "limit": limit,
            "currency": currency,
            "token": self.token,
        }
        if return_date:
            params["return_at"] = return_date      # YYYY-MM-DD или YYYY-MM
            params["one_way"] = "false"
        else:
            params["one_way"] = "true"
        if self.marker:
            params["marker"] = self.marker

        for attempt in range(3):
            try:
                r = requests.get(V3_URL, params=params, timeout=self.timeout)
                if r.status_code == 429:
                    log.warning("429 rate limit, сплю 15с (пара %s->%s)",
                                depart_date, return_date)
                    time.sleep(15)
                    continue
                r.raise_for_status()
                resp = r.json()
                data = resp.get("data", [])
                if resp.get("error") or resp.get("success") is False:
                    # невалидный токен/лимиты API приходят с HTTP 200
                    log.warning("TP API error (%s->%s, dep=%s): %s",
                                origin, destination, depart_date,
                                str(resp)[:200])
                    return []
                if not data:
                    log.info("TP API: кэш пуст (dep=%s, ret=%s, %s->%s)",
                             depart_date, return_date, origin, destination)
                return [o for o in data if o.get("price")]
            except requests.RequestException as e:
                log.warning("Ошибка запроса (%s->%s), попытка %d: %s",
                            depart_date, return_date, attempt + 1, e)
                time.sleep(2 * (attempt + 1))
            except ValueError as e:
                log.warning("Невалидный JSON: %s", e)
                return []
        return []


def build_search_url(link: str, marker: str | None = None,
                     carry_on: bool = False) -> str:
    """Собирает deeplink на Aviasales из поля link ответа API.

    link имеет вид "/search/MOW1607OGK31007OGKMOW01081".
    Дополнительно можно добавить маркер партнёра и фильтр ручной клади.
    """
    url = "https://www.aviasales.ru" + (link or "")
    sep = "?" if "?" not in url else "&"
    extra = []
    if carry_on:
        extra.append("filter_hand_luggage=1")
    if marker:
        extra.append(f"marker={marker}")
    return url + (sep + "&".join(extra) if extra else "")
