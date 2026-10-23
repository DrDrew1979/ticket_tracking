"""Поиск городов и аэропортов для меню бота.

Источник — открытый автокомплит Travelpayouts (без токена):
https://autocomplete.travelpayouts.com/places2?term=...&locale=ru
Ответ: [{code, name, type: city|airport, country_name, weight, ...}]

Правила (после Задачи 13): ВСЕГДА показываем до 5 лучших совпадений,
тупика «слишком много совпадений» больше нет. Ранжирование: точное
совпадение названия — первым (город важнее аэропорта, дальше по
популярности weight). Так «Москва» возвращает «Москва — все аэропорты
[MOW]» первой строкой, а не отказ: одноимённых городов с аэропортами
в мире нет, «лишние» совпадения — это аэропорты самой Москвы и шум
нечёткого поиска (например «Мисава», Япония, weight=2).

Формат запроса «город, страна» — для настоящих однофамильцев
(«Мельбурн, Австралия»): указанная страна получает приоритет в ранге.

Подписи вариантов (ревизия диалога 2026-10) — единый шаблон
«Страна, Город (код), …»:
  город:   «Россия, Москва (MOW), все аэропорты»
  аэропорт: «Россия, Москва (MOW), Шереметьево (SVO)»

Если сеть недоступна или автокомплит не нашёл ничего — офлайн-фолбэк
с нечётким поиском (difflib) по стартовому списку популярных пунктов
(учитывает опечатки).
"""
import difflib
import logging

import requests

log = logging.getLogger(__name__)
URL = "https://autocomplete.travelpayouts.com/places2"

# Стартовый офлайн-список: крупные города/аэропорты (RU-фокус) для
# нечёткого поиска при недоступности сети. code — коды Aviasales.
STARTER = {
    "москва": ("MOW", "city"), "домодедово": ("DME", "airport"),
    "шереметьево": ("SVO", "airport"), "внуково": ("VKO", "airport"),
    "санкт-петербург": ("LED", "city"), "пулково": ("LED", "airport"),
    "сочи": ("AER", "city"), "адлер": ("AER", "airport"),
    "горно-алтайск": ("OGK", "city"), "новосибирск": ("OVB", "city"),
    "толмачево": ("OVB", "airport"), "екатеринбург": ("SVX", "city"),
    "кольцово": ("SVX", "airport"), "казань": ("KZN", "city"),
    "нижний новгород": ("GOJ", "city"), "стригино": ("GOJ", "airport"),
    "самара": ("KUF", "city"), "курумоч": ("KUF", "airport"),
    "уфа": ("UFA", "city"), "челябинск": ("CEK", "city"),
    "пермь": ("PEE", "city"), "ростов-на-дону": ("ROV", "city"),
    "краснодар": ("KRR", "city"), "пашковский": ("KRR", "airport"),
    "минеральные воды": ("MRV", "city"), "владикавказ": ("OGZ", "city"),
    "грозный": ("GRV", "city"), "махачкала": ("MCX", "city"),
    "красноярск": ("KJA", "city"), "иркутск": ("IKT", "city"),
    "владивосток": ("VVO", "city"), "хабаровск": ("KHV", "city"),
    "новый уренгой": ("NUX", "city"), "сургут": ("SGC", "city"),
    "тюмень": ("TJM", "city"), "омск": ("OMS", "city"),
    "томск": ("TOF", "city"), "кемерово": ("KEJ", "city"),
    "барнаул": ("BAX", "city"), "новокузнецк": ("NOZ", "city"),
    "абакан": ("ABA", "city"), "кызыл": ("KYZ", "city"),
    "якутск": ("YKS", "city"), "магадан": ("GDX", "city"),
    "петропавловск-камчатский": ("PKC", "city"),
    "южно-сахалинск": ("UUS", "city"), "калининград": ("KGD", "city"),
    "храброво": ("KGD", "airport"), "мурманск": ("MMK", "city"),
    "архангельск": ("ARH", "city"), "сыктывкар": ("SCW", "city"),
    "волгоград": ("VOG", "city"), "астрахань": ("ASF", "city"),
    "саратов": ("RTW", "city"), "воронеж": ("VOZ", "city"),
    "белгород": ("EGO", "city"), "курск": ("URS", "city"),
    "орел": ("OEL", "city"), "тула": ("TYA", "city"),
    "рязань": ("RYB", "city"), "владимир": ("VLD", "city"),
    "тверь": ("KLD", "city"), "ярославль": ("YAR", "city"),
    "иваново": ("IWA", "city"), "киров": ("KVX", "city"),
    "чебоксары": ("CSY", "city"), "йошкар-ола": ("JOK", "city"),
    "саранск": ("SKX", "city"), "пенза": ("PEZ", "city"),
    "ульяновск": ("ULV", "city"), "оренбург": ("REN", "city"),
    "липецк": ("LPK", "city"), "тамбов": ("TBW", "city"),
    "элиста": ("ESL", "city"), "нальчик": ("NAL", "city"),
    "черкесск": ("ACS", "city"), "карачаевск": ("ACS", "city"),
    "магнитогорск": ("MQF", "city"), "нижневартовск": ("NJC", "city"),
    "нижнекамск": ("NBC", "city"), "бегишево": ("NBC", "airport"),
    "анапа": ("AAQ", "city"), "витязево": ("AAQ", "airport"),
    "геленджик": ("GDZ", "city"), "елабуга": ("NBC", "city"),
    "калуга": ("KLF", "city"), "сычёвка": ("KLF", "airport"),
    "мурманск- аэропорт": ("MMK", "airport"),
    "алматы": ("ALA", "city"), "астана": ("NQZ", "city"),
    "бишкек": ("FRU", "city"), "ташкент": ("TAS", "city"),
    "душанбе": ("DYU", "city"), "ершово": ("DYU", "airport"),
    "ерейван": ("EVN", "city"), "звартноц": ("EVN", "airport"),
    "баку": ("GYD", "city"), "тбилиси": ("TBS", "city"),
    "минск": ("MSQ", "city"), "анталья": ("AYT", "city"),
    "стамбул": ("IST", "city"), "дубай": ("DXB", "city"),
    "бангкок": ("BKK", "city"), "пхукет": ("HKT", "city"),
    "пекин": ("PEK", "city"), "шанхай": ("PVG", "city"),
    "дели": ("DEL", "city"), "гоа": ("GOI", "city"),
    "каир": ("CAI", "city"), "тел-авив": ("TLV", "city"),
    "минеральные воды аэропорт": ("MRV", "airport"),
}


def _label(item: dict) -> str:
    """«Страна, Город (код), все аэропорты | Аэропорт (код)»."""
    name = item.get("name") or item.get("code") or "?"
    code = item.get("code") or ""
    country = item.get("country_name") or ""
    if item.get("type") == "city":
        s = f"{name} ({code}), все аэропорты"
    else:
        city = item.get("city_name") or item.get("main_airport_name") or ""
        ccode = item.get("city_code") or ""
        if city and ccode:
            city_part = f"{city} ({ccode}), "
        elif city:
            city_part = f"{city}, "
        else:
            city_part = ""
        s = f"{city_part}{name} ({code})"
    return f"{country}, {s}" if country else s


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("ё", "е").split())[:64]


def _offline(term: str, limit: int):
    """difflib-поиск по стартовому списку (терпит опечатки).

    Возвращает (top-limit лучших, truncated) — как онлайн-ветку.
    """
    main = term.partition(",")[0].strip()
    names = list(STARTER.keys())
    close = difflib.get_close_matches(main, names, n=limit * 2, cutoff=0.62)
    exact = [n for n in names if n == main]
    pref = [n for n in names if n.startswith(main) and n != main]
    ordered = exact + pref + [c for c in close if c not in exact + pref]
    out = []
    for n in ordered[:limit]:
        code, kind = STARTER[n]
        out.append({"code": code,
                    "label": f"{n.title()} ({code}), все аэропорты"
                    if kind == "city" else f"{n.title()} ({code})"})
    return out, len(ordered) > limit


def search(term: str, limit: int = 5, timeout: int = 10):
    """Возвращает (candidates, truncated, error).

    candidates: до limit лучших совпадений, точное название города —
    первым: [{"code": "MOW", "label": "Москва — все аэропорты…"}]
    truncated: True — совпадений больше limit, показаны лучшие
    (клиент показывает мягкое примечание, а не отказ);
    error: True — сервис недоступен и офлайн-фолбэк тоже не помог.
    """
    term = _norm(term)
    if len(term) < 2:
        return [], False, False
    # «город, страна» — уточнение для однофамильцев
    main, _, hint = term.partition(",")
    main, hint = main.strip(), hint.strip()
    if len(main) < 2:
        return [], False, False
    try:
        r = requests.get(
            URL, params={"term": main, "locale": "ru",
                         "types[]": ["city", "airport"]},
            timeout=timeout)
        r.raise_for_status()
        items = r.json() or []
    except Exception as e:  # сеть/JSON — тихий офлайн-фолбэк
        log.warning("autocomplete недоступен: %s", e)
        cands, too = _offline(main, limit)
        if cands:
            return cands, too, False
        return [], False, True  # не нашли и сеть лежит

    seen, res = set(), []
    for it in items:
        code = it.get("code")
        if not code or (code, it.get("type")) in seen:
            continue
        seen.add((code, it.get("type")))
        try:
            weight = int(it.get("weight") or 0)
        except (TypeError, ValueError):
            weight = 0
        res.append({"item": it, "label": _label(it), "code": code,
                    "name": _norm(it.get("name") or ""),
                    "weight": weight})

    if not res:
        cands, too = _offline(main, limit)
        return cands, too, False

    def rank(x):
        nm = x["name"]
        m = (0 if nm == main else 1 if nm.startswith(main)
             else 2 if main in nm else 3)
        if m == 3:  # в названии запроса нет — но это аэропорт ИСКОМОГО города?
            assoc = _norm(x["item"].get("city_name")
                          or x["item"].get("main_airport_name") or "")
            if assoc and (assoc == main or main in assoc or assoc in main):
                m = 2  # аэропорты города — выше постороннего шума (Мисава)
        if hint:  # страна из запроса «город, страна» — сильнее всего остального
            cn = _norm(x["item"].get("country_name") or "")
            if hint not in cn:
                m += 10
        return (m,
                0 if x["item"].get("type") == "city" else 1,
                -x["weight"])

    res.sort(key=rank)
    return ([{"code": x["code"], "label": x["label"]} for x in res[:limit]],
            len(res) > limit, False)
