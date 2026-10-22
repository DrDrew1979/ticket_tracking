"""SQLite-хранилище истории цен и отправленных алертов.

База (prices.db) коммитится обратно в репозиторий GitHub Actions-ом,
 поэтому состояние переживает перезапуски раннеров.
"""
import sqlite3
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,               -- момент запроса (UTC, ISO)
    depart_date TEXT NOT NULL,      -- YYYY-MM-DD вылет туда
    return_date TEXT NOT NULL,      -- YYYY-MM-DD вылет обратно
    price INTEGER NOT NULL,         -- RUB, туда-обратно за 1 пассажира
    airline TEXT,
    transfers INTEGER,
    return_transfers INTEGER,
    duration_to_min INTEGER,        -- длительность туда, минут
    duration_back_min INTEGER,      -- длительность обратно, минут
    departure_at TEXT,              -- ISO с местным смещением аэропорта
    return_at TEXT,
    link TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_pair_ts
    ON observations (depart_date, return_date, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,             -- min30 | cutoff
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    price INTEGER,
    link TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_pair_ts
    ON alerts (depart_date, return_date, ts);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ---------------- observations ----------------

    def store_offers(self, depart_date: str, return_date: str, offers: list):
        now = datetime.utcnow().isoformat(timespec="seconds")
        rows = [
            (
                now, depart_date, return_date, int(o["price"]),
                o.get("airline"),
                _int_or_none(o.get("transfers")),
                _int_or_none(o.get("return_transfers")),
                _int_or_none(o.get("duration_to")),
                _int_or_none(o.get("duration_back")),
                o.get("departure_at"),
                o.get("return_at"),
                o.get("link"),
            )
            for o in offers
        ]
        self.conn.executemany(
            """INSERT INTO observations
               (ts, depart_date, return_date, price, airline, transfers,
                return_transfers, duration_to_min, duration_back_min,
                departure_at, return_at, link)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        self.conn.commit()

    def distinct_days(self, depart_date: str, return_date: str) -> int:
        cur = self.conn.execute(
            """SELECT COUNT(DISTINCT date(ts)) FROM observations
               WHERE depart_date=? AND return_date=?""",
            (depart_date, return_date),
        )
        return cur.fetchone()[0]

    def history_min(self, depart_date: str, return_date: str, days: int):
        """Минимальная цена за последние N дней запросов (по всем наблюдениям)."""
        cur = self.conn.execute(
            """SELECT MIN(price) FROM observations
               WHERE depart_date=? AND return_date=?
                 AND ts >= datetime('now', ?)""",
            (depart_date, return_date, f"-{days} days"),
        )
        v = cur.fetchone()[0]
        return int(v) if v is not None else None

    def daily_min_series(self, depart_date: str, return_date: str, days: int):
        """[(дата, минимальная цена за день)] по возрастанию даты."""
        cur = self.conn.execute(
            """SELECT date(ts) AS d, MIN(price) FROM observations
               WHERE depart_date=? AND return_date=?
                 AND ts >= datetime('now', ?)
               GROUP BY d ORDER BY d""",
            (depart_date, return_date, f"-{days} days"),
        )
        return [(r[0], int(r[1])) for r in cur.fetchall()]

    def latest_offers_today(self, depart_date: str, return_date: str):
        """Лучшие предложения из последней сегодняшего наблюдения пары."""
        cur = self.conn.execute(
            """SELECT * FROM observations
               WHERE depart_date=? AND return_date=?
                 AND date(ts) = date('now')
                 AND ts = (SELECT MAX(ts) FROM observations
                           WHERE depart_date=? AND return_date=?
                             AND date(ts) = date('now'))""",
            (depart_date, return_date, depart_date, return_date),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------------- alerts ----------------

    def recent_alert(self, kind: str, depart_date: str, return_date: str,
                     hours: int):
        """Последний алерт этого типа по паре за N часов (или None)."""
        cur = self.conn.execute(
            """SELECT price, ts FROM alerts
               WHERE kind=? AND depart_date=? AND return_date=?
                 AND ts >= datetime('now', ?)
               ORDER BY ts DESC LIMIT 1""",
            (kind, depart_date, return_date, f"-{hours} hours"),
        )
        row = cur.fetchone()
        return {"price": row[0], "ts": row[1]} if row else None

    def record_alert(self, kind: str, depart_date: str, return_date: str,
                     price, link):
        self.conn.execute(
            """INSERT INTO alerts (ts, kind, depart_date, return_date, price, link)
               VALUES (datetime('now'), ?,?,?,?,?)""",
            (kind, depart_date, return_date, price, link),
        )
        self.conn.commit()


def _int_or_none(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
