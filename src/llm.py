"""LLM-шутка к подтверждению отслеживания через Z.ai (GLM, OpenAI-API).

Шутка генерируется ПОД КОНКРЕТНОГО пользователя: город вылета → город
прилёта + время года, вычисленное из месяца вылета в его параметрах
отслеживания. Офлайн-шаблонов больше нет — только живая генерация.

Модель: бесплатная Flash-модель Z.ai (OpenAI-совместимый API,
endpoint https://api.z.ai/api/paas/v4/chat/completions).

Принципы надежности (фича декоративная и НЕ должна ломать бот):
- ZAI_API_KEY не задан -> шутки нет, подтверждение обычное (CI/офлайн);
- таймаут 5 с или любая ошибка сети/API -> шутки нет;
- НИКАКИХ сообщений об ошибке пользователь не видит: подтверждение
  выглядит так, будто шутки и не планировалось;
- длина жёстко ограничена 250 знаками с пробелами: промпт требует
  короткий ответ, а _trim() страхует, даже если модель нарушит лимит
  (обрезка по границе предложения, иначе — по границе слова).

Переопределение без правки кода (env): ZAI_BASE_URL, ZAI_MODEL.
"""
import json
import os
import re
import urllib.request

MAX_LEN = 250
DEFAULT_TIMEOUT = 5.0

_SYS = ("Ты — автор коротких остроумных шуток для Telegram-бота "
        "авиабилетов. Отвечай ТОЛЬКО текстом шутки на русском языке: "
        "без кавычек, без эмодзи, без markdown, без пояснений и без "
        "вводных слов.")

_TPL = ("Придумай одну короткую шутку (строго не длиннее {n} знаков с "
        "пробелами) про авиапутешествие: вылет из {o}, прилёт в {d}, "
        "поездка {s}. Тёплый лёгкий юмор про самолёты, аэропорты, "
        "чемоданы, отпуск или сезон. Без чёрного юмора.")


def short_place(label: str | None) -> str:
    """«Россия, Москва (MOW), все аэропорты» -> «Москва»."""
    s = (label or "").split("(")[0].strip().rstrip(",")
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) >= 2:
        return parts[1] if parts[1].lower() not in ("россия",) else parts[0]
    return parts[0] if parts else (label or "туда")


def _season(m: int) -> str:
    """Номер месяца (1-12) -> наречие времени года для промпта."""
    return {12: "зимой", 1: "зимой", 2: "зимой",
            3: "весной", 4: "весной", 5: "весной",
            6: "летом", 7: "летом", 8: "летом",
            9: "осенью", 10: "осенью", 11: "осенью"}.get(int(m), "летом")


def _base() -> str:
    return (os.environ.get("ZAI_BASE_URL", "")
            or "https://api.z.ai/api/paas/v4").rstrip("/")


def _model() -> str:
    return os.environ.get("ZAI_MODEL", "") or "glm-4.5-flash"


def _post(body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        _base() + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer "
                 + (os.environ.get("ZAI_API_KEY") or "").strip()},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _clean(text: str) -> str:
    """Снять мусор вокруг текста шутки (кавычки, markdown, пробелы)."""
    t = (text or "").strip()
    t = re.sub(r"\*+", "", t)          # **markdown** если модель любит
    t = t.strip(" \t\n\r\"'`«»„“”")
    return t


def _trim(text: str, n: int = MAX_LEN) -> str:
    """Страховочная обрезка до n знаков по границе предложения/слова."""
    t = (text or "").strip()
    if len(t) <= n:
        return t
    cut = t[:n]
    for i in range(len(cut) - 1, 10, -1):      # ближайшая точка внутри
        if cut[i] in ".!?":
            return cut[:i + 1]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > 10 else cut


def make(origin_name: str | None, dest_name: str | None, month: int,
         timeout: float = DEFAULT_TIMEOUT) -> str:
    """Шутка ≤ 250 знаков про маршрут и сезон. Пусто — если не вышло.

    Никогда не бросает исключений и не печатает ошибок: при любой
    проблеме возвращается "", и подтверждение отправляется без шутки.
    """
    if not (os.environ.get("ZAI_API_KEY") or "").strip():
        return ""                      # ключа нет — фича отключена
    try:
        o = short_place(origin_name)
        d = short_place(dest_name)
        body = {"model": _model(),
                "messages": [
                    {"role": "system", "content": _SYS},
                    {"role": "user",
                     "content": _TPL.format(n=MAX_LEN, o=o, d=d,
                                            s=_season(month))}],
                "temperature": 1.0,
                "max_tokens": 200}
        data = _post(body, timeout)
        text = _clean(data["choices"][0]["message"]["content"] or "")
    except Exception:
        return ""                      # тишина: таймаут/сеть/API
    return _trim(text) if text else ""
