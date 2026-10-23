"""Диалог настройки отслеживания (мастер из 9 шагов по ТЗ).

Состояние диалога шифруется (dialog_enc) и живёт в БД — каждый запуск
workflow это новый процесс, поэтому State Machine обязана быть внешней.

Везде, где есть значение по умолчанию, «0» принимает его. /cancel
прерывает мастер без сохранения.
"""
import logging
from datetime import date, datetime, timedelta

from . import places

log = logging.getLogger(__name__)

STEPS_ONEWAY = ["city_from", "pick_from", "city_to", "pick_to", "trip",
                "out_date", "out_days", "out_t1", "out_t2",
                "cutoff", "target", "dig_every", "dig_time", "confirm"]


# ---------------------------------------------------------------- парсеры

def parse_date(s: str, default: date | None = None) -> date | None:
    """DD.MM.YYYY | DD.MM.YY | DD.MM | 0 (дефолт). None — ошибка."""
    s = (s or "").strip()
    if s == "0" and default:
        return default
    parts = s.replace("-", ".").split(".")
    if not (2 <= len(parts) <= 3) or not all(p.isdigit() for p in parts):
        return None
    d, m = int(parts[0]), int(parts[1])
    today = date.today()
    if len(parts) == 2:
        y = today.year
        try:
            dt = date(y, m, d)
        except ValueError:
            return None
        if dt < today:
            try:
                dt = date(y + 1, m, d)
            except ValueError:
                return None
        return dt
    y = int(parts[2])
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_time(s: str, default: str | None = None) -> str | None:
    s = (s or "").strip()
    if s == "0" and default:
        return default
    parts = s.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------- запуск

def begin(db, crypto, uid: str) -> str:
    _save(db, crypto, uid, {"st": "city_from", "draft": {}})
    return (
        "🧭 <b>Настройка отслеживания (1/9): город вылета</b>\n\n"
        "Введите город (или аэропорт) вылета — например «Москва» или "
        "«Шереметьево». Можно писать с опечатками.\n"
        "Найду от 1 до 5 вариантов и предложу выбрать номер.\n"
        "<i>Прервать: /cancel</i>"
    )


def _save(db, crypto, uid, state):
    db.update_user(uid, dialog_enc=crypto.enc(state) if state else None)


def _load(crypto, row) -> dict:
    return crypto.dec(row["dialog_enc"]) or {}


def summary(t: dict) -> str:
    out = [
        "📋 <b>Проверьте параметры:</b>",
        f"1. Откуда: {t['origin_name']}",
        f"2. Куда: {t['dest_name']}",
        f"3. Тип: {'только туда' if t['oneway'] else 'туда и обратно'}",
        f"4. Вылет: {_win(t['out'])}",
    ]
    if not t["oneway"]:
        out.append(f"5. Возврат: {_win(t['ret'])}")
    out += [
        f"6. Отсечка (купить обязательно): {_d(t['cutoff'])}",
        f"7. Целевая цена: {t['target']:,} ₽".replace(",", " "),
        f"8. Дайджест: каждые {t['digest_every']} дн. в {t['digest_time']} МСК",
        "",
        "Сохранить? <b>да</b> / <b>нет</b> (нет — начнём заново)",
    ]
    return "\n".join(out)


def _win(w) -> str:
    d0 = date.fromisoformat(w["from"])
    d1 = d0 + timedelta(days=w["days"] - 1)
    if w["days"] == 1:
        return f"{d0:%d.%m.%Y}, {w['t1']}–{w['t2']}"
    return f"{d0:%d.%m.%Y} ({w['t1']}) – {d1:%d.%m.%Y} ({w['t2']})"


def _d(iso) -> str:
    return f"{date.fromisoformat(iso):%d.%m.%Y}"


# ---------------------------------------------------------------- ввод

def feed(db, crypto, uid: str, text: str) -> list[str]:
    row = db.get_user(uid)
    st = _load(crypto, row)
    if not st:
        return ["Отправьте /watch, чтобы настроить отслеживание."]
    st["draft"] = st.get("draft") or {}
    fn = globals().get("_h_" + st["st"])
    try:
        replies = fn(db, crypto, uid, st, text.strip())
    except Exception as e:  # защита от каскада ошибок в диалоге
        log.exception("dialog error: %s", e)
        _save(db, crypto, uid, None)
        return ["⚠️ Ошибка диалога, мастер перезапущен. Отправьте /watch."]
    if isinstance(replies, tuple):  # (draft, "SAVE") от шага confirm
        return [replies]
    if isinstance(replies, str):    # защита от «голых» строк в хендлерах
        return [replies]
    return replies


def _h_city_from(db, crypto, uid, st, text):
    return _place_step(db, crypto, uid, st, text, "origin",
                       "город вылета", "city_to")


def _h_city_to(db, crypto, uid, st, text):
    return _place_step(db, crypto, uid, st, text, "dest",
                       "город прилета", "trip")


def _place_step(db, crypto, uid, st, text, field, title, next_st):
    cands, too_many, error = places.search(text)
    if error:
        return ["🛰 Сервис поиска городов недоступен, попробуйте позже. "
                "Или введите название ещё раз."]
    if too_many:
        return [f"🔍 По запросу «{text}» слишком много совпадений — "
                f"уточните {title} (проверьте ввод и повторите)."]
    if not cands:
        return ["🤷 Не нашёл. Проверьте название и повторите ввод "
                "(город или аэропорт, можно с опечатками)."]
    if len(cands) == 1:
        st["draft"][field] = cands[0]["code"]
        st["draft"][field + "_name"] = cands[0]["label"]
        st["st"] = next_st
        _save(db, crypto, uid, st)
        if next_st == "trip":
            return ["3/9. Тип поездки: введите <b>1</b> (только туда) или "
                    "<b>2</b> (туда и обратно, по умолчанию — 0)."]
        return ["2/9. Город (или аэропорт) прилета — введите название "
                "(с опечатками тоже справлюсь)."]
    st["cand"] = cands
    st["pick_for"] = field
    st["st"] = "pick"
    _save(db, crypto, uid, st)
    lines = ["Выберите вариант (ответьте номером):"]
    lines += [f"{i}. {c['label']}" for i, c in enumerate(cands, 1)]
    return ["\n".join(lines)]


def _h_pick(db, crypto, uid, st, text):
    cands = st.get("cand") or []
    if not text.isdigit() or not (1 <= int(text) <= len(cands)):
        return ["Введите номер от 1 до "
                f"{len(cands)} (или /cancel)."]
    c = cands[int(text) - 1]
    field = st["pick_for"]
    st["draft"][field] = c["code"]
    st["draft"][field + "_name"] = c["label"]
    st["cand"] = None
    st["st"] = "trip" if field == "dest" else "city_to"
    _save(db, crypto, uid, st)
    if st["st"] == "city_to":
        return ["2/9. Город (или аэропорт) прилета — введите название."]
    return ["3/9. Тип поездки: <b>1</b> — только туда, "
            "<b>2</b> — туда и обратно (0 — по умолчанию: туда и обратно)."]


def _h_trip(db, crypto, uid, st, text):
    if text == "0":
        text = "2"
    if text not in ("1", "2"):
        return ["Введите 1 (только туда) или 2 (туда и обратно)."]
    st["draft"]["oneway"] = text == "1"
    st["st"] = "out_date"
    _save(db, crypto, uid, st)
    default = date.today() + timedelta(days=60)
    return [f"4.1/9. Дата начала окна вылета (ДД.ММ.ГГГГ). "
            f"0 — по умолчанию: {default:%d.%m.%Y} (+60 дней)."]


def _h_out_date(db, crypto, uid, st, text):
    d = parse_date(text, date.today() + timedelta(days=60))
    if not d:
        return ["Не понял дату. Формат ДД.ММ.ГГГГ (или ДД.ММ)."]
    if d < date.today():
        return ["Дата уже прошла — введите будущую дату."]
    st["draft"]["out"] = {"from": d.isoformat(), "days": 3,
                          "t1": "00:00", "t2": "23:59"}
    st["st"] = "out_days"
    _save(db, crypto, uid, st)
    return ["4.2/9. Величина окна вылета: <b>1</b>, <b>2</b>, <b>3</b> или "
            "<b>4</b> дня подряд. 0 — по умолчанию: 3 дня (+2 к дате)."]


def _days_step(db, crypto, uid, st, text, wkey, next_st):
    if text == "0":
        text = "3"
    if text not in ("1", "2", "3", "4"):
        return ["Введите число дней окна: 1, 2, 3 или 4."]
    st["draft"][wkey]["days"] = int(text)
    st["st"] = next_st
    _save(db, crypto, uid, st)
    w = st["draft"][wkey]
    return [f"4.3/9. Время вылета «не ранее, чем» в первый день окна "
            f"(ЧЧ:ММ). 0 — по умолчанию: 00:00." if wkey == "out" else
            f"5.3/9. Время вылета обратно «не ранее, чем» (ЧЧ:ММ). "
            f"0 — по умолчанию: 00:00."]


def _h_out_days(db, crypto, uid, st, text):
    return _days_step(db, crypto, uid, st, text, "out", "out_t1")


def _h_out_t1(db, crypto, uid, st, text):
    t = parse_time(text, "00:00")
    if not t:
        return ["Не понял время. Формат ЧЧ:ММ, например 20:00."]
    st["draft"]["out"]["t1"] = t
    st["st"] = "out_t2"
    _save(db, crypto, uid, st)
    return ["4.4/9. Время вылета «не позднее, чем» в последний день окна "
            "(ЧЧ:ММ). 0 — по умолчанию: 23:59."]


def _h_out_t2(db, crypto, uid, st, text):
    t = parse_time(text, "23:59")
    if not t:
        return ["Не понял время. Формат ЧЧ:ММ, например 23:59."]
    out = st["draft"]["out"]
    if out["days"] == 1 and t <= out["t1"]:
        return [f"Время «не позднее» должно быть больше, чем "
                f"{out['t1']} (время «не ранее»). Повторите ввод."]
    out["t2"] = t
    st["st"] = "ret_date" if not st["draft"]["oneway"] else "cutoff"
    _save(db, crypto, uid, st)
    if st["st"] == "cutoff":
        return _cutoff_prompt(st)
    d0 = date.fromisoformat(out["from"])
    d1 = d0 + timedelta(days=out["days"] - 1)
    default = date.fromisoformat(out["from"]) + timedelta(days=14)
    default = max(default, d1)
    return [f"5.1/9. Дата начала окна возврата (ДД.ММ.ГГГГ). "
            f"0 — по умолчанию: {default:%d.%m.%Y} "
            f"(+14 дней от даты начала окна вылета; не ранее {d1:%d.%m.%Y})."]


def _h_ret_date(db, crypto, uid, st, text):
    out = st["draft"]["out"]
    d1 = date.fromisoformat(out["from"]) + timedelta(days=out["days"] - 1)
    default = max(date.fromisoformat(out["from"]) + timedelta(days=14), d1)
    d = parse_date(text, default)
    if not d:
        return ["Не понял дату. Формат ДД.ММ.ГГГГ (или ДД.ММ)."]
    if d < d1:
        return [f"Введите дату не ранее {_d(d1.isoformat())} "
                f"(дата окончания окна вылета)."]
    st["draft"]["ret"] = {"from": d.isoformat(), "days": 3,
                          "t1": "00:00", "t2": "23:59"}
    st["st"] = "ret_days"
    _save(db, crypto, uid, st)
    return ["5.2/9. Величина окна возврата: 1, 2, 3 или 4 дня. "
            "0 — по умолчанию: 3 дня."]


def _h_ret_days(db, crypto, uid, st, text):
    return _days_step(db, crypto, uid, st, text, "ret", "ret_t1")


def _h_ret_t1(db, crypto, uid, st, text):
    t = parse_time(text, "00:00")
    if not t:
        return ["Не понял время. Формат ЧЧ:ММ."]
    st["draft"]["ret"]["t1"] = t
    st["st"] = "ret_t2"
    _save(db, crypto, uid, st)
    return ["5.4/9. Время вылета обратно «не позднее, чем» в последний "
            "день окна (ЧЧ:ММ). 0 — по умолчанию: 23:59."]


def _h_ret_t2(db, crypto, uid, st, text):
    t = parse_time(text, "23:59")
    if not t:
        return ["Не понял время. Формат ЧЧ:ММ."]
    ret = st["draft"]["ret"]
    if ret["days"] == 1 and t <= ret["t1"]:
        return [f"Время «не позднее» должно быть больше, чем {ret['t1']}. "
                f"Повторите ввод."]
    ret["t2"] = t
    st["st"] = "cutoff"
    _save(db, crypto, uid, st)
    return [_cutoff_prompt(st)]


def _cutoff_prompt(st) -> str:
    d0 = date.fromisoformat(st["draft"]["out"]["from"])
    default = d0 - timedelta(days=7)
    return (f"6/9. Дата отсечки — купить билет нужно в любом случае до этой "
            f"даты (ДД.ММ.ГГГГ). 0 — по умолчанию: {default:%d.%m.%Y} "
            f"(за 7 дней до начала окна вылета).")


def _h_cutoff(db, crypto, uid, st, text):
    d0 = date.fromisoformat(st["draft"]["out"]["from"])
    d = parse_date(text, d0 - timedelta(days=7))
    if not d:
        return ["Не понял дату. Формат ДД.ММ.ГГГГ."]
    if d > d0:
        return [f"Дата отсечки должна быть не позже начала окна вылета "
                f"({_d(d0.isoformat())})."]
    st["draft"]["cutoff"] = d.isoformat()
    st["st"] = "target"
    _save(db, crypto, uid, st)
    return ["7/9. Целевая цена в рублях (общая, с учетом типа поездки): "
            "как только цена станет НИЖЕ, пришлю немедленное уведомление. "
            "0 — по умолчанию: 1 ₽ (уведомления по цене выключены, "
            "алерты «минимум за 30 дней» с целью 1 ₽ не сработают)."]


def _h_target(db, crypto, uid, st, text):
    if text == "0":
        text = "1"
    if not text.isdigit() or int(text) < 1 or int(text) > 10_000_000:
        return ["Введите целое число рублей (например 25000)."]
    st["draft"]["target"] = int(text)
    st["st"] = "dig_every"
    _save(db, crypto, uid, st)
    return ["8/9. Частота дайджеста (в днях): 1, 3, 7 или 14. "
            "0 — по умолчанию: 1 (ежедневно). Срок отсчитывается от "
            "даты запуска бота."]


def _h_dig_every(db, crypto, uid, st, text):
    if text == "0":
        text = "1"
    if text not in ("1", "3", "7", "14"):
        return ["Введите 1, 3, 7 или 14."]
    st["draft"]["digest_every"] = int(text)
    st["st"] = "dig_time"
    _save(db, crypto, uid, st)
    return ["8.1/9. Время дайджеста по МСК (ЧЧ:ММ). "
            "0 — по умолчанию: 09:00."]


def _h_dig_time(db, crypto, uid, st, text):
    t = parse_time(text, "09:00")
    if not t:
        return ["Не понял время. Формат ЧЧ:ММ по МСК."]
    st["draft"]["digest_time"] = t
    st["st"] = "confirm"
    _save(db, crypto, uid, st)
    return [summary(st["draft"])]


def _h_confirm(db, crypto, uid, st, text):
    t = text.lower()
    if t in ("нет", "no", "n"):
        st["st"] = "city_from"
        st["draft"] = {}
        _save(db, crypto, uid, st)
        return ["Хорошо, начнём заново. 1/9. Город вылета?"]
    if t in ("да", "yes", "y"):
        draft = st["draft"]
        _save(db, crypto, uid, None)  # диалог завершён
        return draft, "SAVE"  # сигнал наружу: bot.py вызовет finish_save
    return ["Ответьте «да» или «нет»."]


def finish_save(db, crypto, uid, admin_uid_str: str, draft: dict,
                cfg_mu: dict) -> str:
    """Активация после «да»: 'ok' | 'waitlist'."""
    from . import users as U
    return U.save_tracking(db, crypto, uid, admin_uid_str, draft, cfg_mu)


def _h_wl(db, crypto, uid, st, text):
    return ["⏳ Вы в листе ожидания — активных мест сейчас нет. Ваши "
            "параметры сохранены: когда место освободится, пришлю "
            "сообщение (или попробуйте /retry). "
            "Настроить заново: /cancel → /watch."]


def _h_stop_confirm(db, crypto, uid, st, text):
    from . import users as U
    t = text.lower()
    if t in ("да", "yes", "y"):
        U.remove_tracking(db, crypto, uid)
        _save(db, crypto, uid, None)
        return ["🗑 Отслеживание удалено. Настроить новое: /watch"]
    if t in ("нет", "no", "n"):
        _save(db, crypto, uid, None)
        return ["Отменено — отслеживание продолжает работать."]
    return ["Ответьте «да» или «нет»."]


def request_stop(db, crypto, uid: str) -> str:
    _save(db, crypto, uid, {"st": "stop_confirm", "draft": {}})
    return ("🚫 Удалить отслеживание полностью? "
            "Ответьте <b>да</b> или <b>нет</b>.")


def save_waitlist_draft(db, crypto, uid: str, draft: dict):
    _save(db, crypto, uid, {"st": "wl", "draft": draft})


def try_retry(db, crypto, uid: str, admin_uid_str: str,
              cfg_mu: dict) -> str:
    """Активация сохранённых параметров, когда появилось место."""
    row = db.get_user(uid)
    st = _load(crypto, row)
    if st.get("st") != "wl" or not st.get("draft"):
        return "none"
    from . import users as U
    res = U.save_tracking(db, crypto, uid, admin_uid_str, st["draft"], cfg_mu)
    if res == "ok":
        _save(db, crypto, uid, None)
    return res
