"""Умный режим: ротация пар дат между запусками.

Зачем: число пар = (дней в окне туда) x (дней в окне обратно) и растёт
перемножением. Если расширить окна (например, неделя x неделя = 35 пар),
опрос ВСЕХ пар каждые 30 минут умножит нагрузку на API. Вместо этого
пары делятся на группы по pairs_per_run, и каждый прогон опрашивает
одну группу по кругу. Дайджест при этом всегда показывает полную
картину по всем парам (он всего раз в день).

Счётчик ротации хранится в prices.db (таблица meta) и коммитится в
репозиторий вместе с базой, поэтому круг не сбивается ни при пропущенных
запусках cron, ни при смене раннера.
"""
import logging
import math

log = logging.getLogger(__name__)
COUNTER_KEY = "rotation_counter"


def max_pairs_per_run(cfg: dict) -> int:
    """pairs_per_run из конфига; 0 = ограничение выключено."""
    raw = (cfg.get("monitor") or {}).get("pairs_per_run")
    try:
        n = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        log.warning("pairs_per_run=%r не число — игнорирую", raw)
        return 0
    return max(n, 0)


def select_pairs(cfg: dict, db, pairs: list) -> tuple[list, str]:
    """Вернуть (пары этого прогона, пояснение для лога).

    pairs_per_run: null / 0 / >= числа пар  -> все пары каждый прогон;
    pairs_per_run: N                        -> группа из N пар по кругу.
    """
    n = max_pairs_per_run(cfg)
    total = len(pairs)
    if n <= 0 or n >= total:
        return pairs, f"все {total} пар(ы) за прогон (ротация выключена)"

    groups = math.ceil(total / n)
    try:
        counter = int(db.get_meta(COUNTER_KEY, "0") or 0)
    except (TypeError, ValueError):
        counter = 0
    idx = counter % groups
    db.set_meta(COUNTER_KEY, counter + 1)

    selected = pairs[idx * n:(idx + 1) * n]
    note = (f"группа {idx + 1}/{groups}: {len(selected)} пар(ы) из {total} "
            f"(pairs_per_run={n}); каждая пара проверяется раз в "
            f"~{groups * 30} мин при cron */30")
    log.info("Ротация: %s", note)
    return selected, note
