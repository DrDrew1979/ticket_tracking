"""LLM-шутка к подтверждению отслеживания через Z.ai (GLM, OpenAI-API).

Шутка генерируется ПОД КОНКРЕТНОГО пользователя: город вылета → город
прилёта + время года, вычисленное из месяца вылета в его параметрах
отслеживания. Офлайн-шаблонов больше нет — только живая генерация.

Модель: бесплатная Flash-модель Z.ai (OpenAI-совместимый API,
endpoint https://api.z.ai/api/paas/v4/chat/completions).

ВАЖНО (причина «шутка не появляется» в версии с таймаутом 5 с):
у GLM-4.5 (включая Flash) режим размышлений (thinking) включён ПО
УМОЛЧАНИЮ — модель тратила весь бюджет max_tokens на рассуждения
(reasoning_content), поле content приходило пустым, плюс режим
размышлений замедлял ответ и приводил к таймауту. Поэтому запрос ЯВНО
шлёт "thinking": {"type": "disabled"} (нестандартный параметр Z.ai,
OpenAI-клиенты его просто игнорируют).

Принципы надежности (фича декоративная и НЕ должна ломать бот):
- ZAI_API_KEY не задан -> шутки нет, подтверждение обычное (CI/офлайн);
- таймаут или любая ошибка сети/API -> шутки нет;
- НИКАКИХ сообщений об ошибке пользователь не видит: подтверждение
  выглядит так, будто шутки и не планировалось;
- причина отказа ВСЕГДА пишется в лог (log.warning) — в GitHub Actions
  её видно в логах прогона tg-live-poll (поиск по «LLM»);
- длина жёстко ограничена 250 знаками с пробелами: промпт требует
  короткий ответ, а _trim() страхует, даже если модель нарушит лимит
  (обрезка по границе предложения, иначе — по границе слова).

Переопределение без правки кода (env): ZAI_BASE_URL, ZAI_MODEL,
ZAI_TIMEOUT (секунды, по умолчанию 8).
"""
import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

MAX_LEN = 250
DEFAULT_TIMEOUT = 8.0

_SYS = ("Ты — острослов и автор афоризмов, пишешь короткие шутки для "
        "Telegram-бота дешёвых авиабилетов. Твой юмор — ирония, "
        "неожиданный поворот мысли, игра слов, точные наблюдения. Черпай "
        "образы в широком культурном контексте: классическая и "
        "современная литература, кино и сериалы, мифы, история, мемы и "
        "тренды интернета, общеизвестные события. Избегай штампов "
        "(задержки рейсов, потерянные чемоданы, «дешевле только "
        "пешком»), банальных анекдотов и пошлости. Уровень сюжета — как "
        "в примерах, но приёмы и сюжеты каждый раз разные: «Данте "
        "девять кругов ада придумал бы быстрее, полетав с двумя "
        "пересадками по самому дешёвому тарифу»; «Гэндальф говорил «ты "
        "не пройдёшь» — так он описывал контроль на посадке». Отвечай "
        "ТОЛЬКО текстом шутки на русском языке: без кавычек, без эмодзи, "
        "без markdown, без пояснений и без вводных слов.")

_TPL = ("Одна короткая шутка (строго не длиннее {n} знаков с пробелами) "
        "для человека, который ищет билеты: вылет из {o}, прилёт в {d}, "
        "поездка {s}. Форму выбери сам: афоризм, мини-диалог, "
        "псевдоцитата персонажа книги или фильма, каламбур, абсурдная "
        "логика, неожиданное сравнение. Отсылка должна быть узнаваемой "
        "и по делу, а не приклеенной для галочки. Без чёрного юмора, "
        "политики, национальных тем и всего, что может обидеть.")


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
         timeout: float | None = None) -> str:
    """Шутка ≤ 250 знаков про маршрут и сезон. Пусто — если не вышло.

    Никогда не бросает исключений и не роняет бот: при любой проблеме
    возвращается "", а причина пишется в лог (видно в Actions).
    """
    if not (os.environ.get("ZAI_API_KEY") or "").strip():
        log.info("LLM: ZAI_API_KEY не задан — шутка пропущена")
        return ""
    timeout = timeout if timeout is not None else \
        float(os.environ.get("ZAI_TIMEOUT") or DEFAULT_TIMEOUT)
    try:
        o = short_place(origin_name)
        d = short_place(dest_name)
        body = {"model": _model(),
                "messages": [
                    {"role": "system", "content": _SYS},
                    {"role": "user",
                     "content": _TPL.format(n=MAX_LEN, o=o, d=d,
                                            s=_season(month))}],
                "thinking": {"type": "disabled"},
                "temperature": 1.0,
                "max_tokens": 300}
        data = _post(body, timeout)
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = _clean(msg.get("content") or "")
        if not text:
            reason = ("есть reasoning_content, пустой content (thinking "
                      "не отключился?)" if msg.get("reasoning_content")
                      else f"пустой ответ: {str(data)[:200]}")
            log.warning("LLM: модель вернула пустой content — %s", reason)
            return ""
    except urllib.error.HTTPError as e:
        log.warning("LLM: HTTP %s — %s", e.code, (e.read()[:150] or b""))
        return ""
    except Exception as e:  # тишина для пользователя, диагноз — в лог
        log.warning("LLM: запрос не удался (%s: %s)",
                    type(e).__name__, str(e)[:150])
        return ""
    log.info("LLM: шутка получена (%d знаков)", len(text))
    return _trim(text) if text else ""
