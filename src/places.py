"""Поиск городов и аэропортов для меню бота.

Источник — открытый автокомплит Travelpayouts (без токена):
https://autocomplete.travelpayouts.com/places2?term=...&locale=ru
Ответ: [{code, name, type: city|airport, country_name, weight, ...}]

Правила по ТЗ: 1–5 совпадений -> нумерованный список; больше 5 ->
просьба уточнить ввод. Если сеть недоступна или автокомплит не нашёл
ничего — офлайн-фолбэк с нечётким поиском (difflib) по стартовому
списку популярных пунктов (учитывает опечатки).
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
    name = item.get("name") or item.get("code") or "?"
    country = item.get("country_name") or ""
    code = item.get("code") or ""
    if item.get("type") == "city":
        return f"{name} — все аэропорты [{code}] ({country})".replace(" ()", "")
    sub = item.get("city_name") or item.get("main_airport_name") or ""
    parts = [name + f" [{code}]"]
    if sub and sub != name:
        parts.append(sub)
    if country:
        parts.append(country)
    return ", ".join(parts)


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())[:64]


def _offline(term: str, limit: int):
    """difflib-поиск по стартовому списку (терпит опечатки)."""
    names = list(STARTER.keys())
    close = difflib.get_close_matches(term, names, n=limit * 2, cutoff=0.62)
    exact = [n for n in names if n == term]
    pref = [n for n in names if n.startswith(term) and n != term]
    ordered = exact + pref + [c for c in close if c not in exact + pref]
    out = []
    for n in ordered[:limit]:
        code, kind = STARTER[n]
        out.append({"code": code,
                    "label": f"{n.title()} — все аэропорты [{code}]"
                    if kind == "city" else f"{n.title()} [{code}]"})
    return out, len(ordered) > limit


def search(term: str, limit: int = 5, timeout: int = 10):
    """Возвращает (candidates, too_many, error).

    candidates: [{"code": "MOW", "label": "Москва — все аэропорты…"}]
    too_many: True -> просим уточнить ввод; error: True -> сервис недоступен.
    """
    term = _norm(term)
    if len(term) < 2:
        return [], False, False
    try:
        r = requests.get(
            URL, params={"term": term, "locale": "ru",
                         "types[]": ["city", "airport"]},
            timeout=timeout)
        r.raise_for_status()
        items = r.json() or []
    except Exception as e:  # сеть/JSON — тихий офлайн-фолбэк
        log.warning("autocomplete недоступен: %s", e)
        cands, too = _offline(term, limit)
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
                    "name": (it.get("name") or "").lower(),
                    "weight": weight})

    if not res:
        cands, too = _offline(term, limit)
        return cands, too, False

    def rank(x):
        nm = x["name"]
        return (0 if nm == term else 1 if nm.startswith(term)
                else 2 if term in nm else 3,
                0 if x["item"].get("type") == "city" else 1,
                -x["weight"])

    res.sort(key=rank)
    if len(res) > limit:
        return [], True, False
    return [{"code": x["code"], "label": x["label"]} for x in res], False, False
