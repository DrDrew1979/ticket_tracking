"""SQLite-хранилище: история цен, алерты, состояние бота, пользователи.

База (prices.db) коммитится обратно в репозиторий GitHub Actions-ом,
поэтому состояние переживает перезапуски раннеров. Репозиторий может
быть публичным — личные данные пользователей лежат ТОЛЬКО в зашифрованных
полях *_enc (см. crypto.py); uid — необратимый HMAC(chat_id).

Экономия размера (критично: GitHub не принимает файлы > 100 МБ):
- на проверку пары пишется ОДНА строка (лучший отфильтрованный оффер) и
  только если цена изменилась или наступили новые сутки;
- retention: наблюдения старше N дней удаляются, раз в неделю VACUUM.
"""
import logging
import sqlite3
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

OWNER = "__owner__"  # uid личного мониторинга владельца (из config.yaml)

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,               -- момент запроса (UTC, ISO)
    depart_date TEXT NOT NULL,      -- YYYY-MM-DD вылет туда
    return_date TEXT NOT NULL,      -- YYYY-MM-DD вылет обратно ('' = one-way)
    price INTEGER NOT NULL,         -- RUB, за 1 пассажира
    airline TEXT,
    transfers INTEGER,
    return_transfers INTEGER,
    duration_to_min INTEGER,
    duration_back_min INTEGER,
    departure_at TEXT,              -- ISO с местным смещением аэропорта
    return_at TEXT,
    link TEXT,
    uid TEXT NOT NULL DEFAULT '__owner__'
);
CREATE INDEX IF NOT EXISTS idx_obs_pair_ts
    ON observations (depart_date, return_date, ts);
CREATE INDEX IF NOT EXISTS idx_obs_uid_pair_ts
    ON observations (uid, depart_date, return_date, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,             -- min30 | cutoff
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    price INTEGER,
    link TEXT,
    uid TEXT NOT NULL DEFAULT '__owner__'
);
CREATE INDEX IF NOT EXISTS idx_alerts_pair_ts
    ON alerts (depart_date, return_date, ts);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    uid TEXT PRIMARY KEY,           -- HMAC(chat_id)[:16]
    ident_enc TEXT NOT NULL,        -- Fernet(JSON: chat_id, username, имена)
    status TEXT NOT NULL,           -- active | banned
    waitlist INTEGER NOT NULL DEFAULT 0,
    wl_notified INTEGER NOT NULL DEFAULT 0,
    joined TEXT NOT NULL,           -- ISO date первой регистрации
    tracking_enc TEXT,              -- Fernet(JSON трекинга) или NULL
    dialog_enc TEXT,                -- Fernet(JSON состояния визарда) или NULL
    last_check TEXT,                -- ISO datetime UTC последнего опроса пар
    last_digest TEXT                -- ISO date последнего дайджеста
);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
"""


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self):
        """Добавление uid в старые таблицы (идемпотентно)."""
        for table in ("observations", "alerts"):
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")]
            if "uid" not in cols:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN uid TEXT NOT NULL "
                    f"DEFAULT '{OWNER}'")
                log.info("Миграция: %s.uid добавлен", table)

    def close(self):
        self.conn.commit()
        self.conn.close()

    # ---------------- meta ----------------

    def get_meta(self, key: str, default=None):
        cur = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value):
        self.conn.execute(
            """INSERT INTO meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (key, str(value)),
        )
        self.conn.commit()

    # ---------------- observations ----------------

    def _last_obs(self, uid: str, d: str, r: str):
        cur = self.conn.execute(
            """SELECT price, date(ts) AS day FROM observations
               WHERE uid=? AND depart_date=? AND return_date=?
               ORDER BY ts DESC LIMIT 1""", (uid, d, r))
        return cur.fetchone()

    def store_best(self, uid: str, depart_date: str, return_date: str,
                   offer: dict) -> bool:
        """Change-only: пишем лучший оффер, если цена изменилась или новый день.

        Возвращает True, если строка записана.
        """
        price = int(offer["price"])
        last = self._last_obs(uid, depart_date, return_date)
        today = datetime.utcnow().date().isoformat()
        if last and int(last["price"]) == price and last["day"] == today:
            return False
        now = datetime.utcnow().isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO observations
               (ts, depart_date, return_date, price, airline, transfers,
                return_transfers, duration_to_min, duration_back_min,
                departure_at, return_at, link, uid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, depart_date, return_date, price,
             offer.get("airline"),
             _int_or_none(offer.get("transfers")),
             _int_or_none(offer.get("return_transfers")),
             _int_or_none(offer.get("duration_to")),
             _int_or_none(offer.get("duration_back")),
             offer.get("departure_at"), offer.get("return_at"),
             offer.get("link"), uid),
        )
        self.conn.commit()
        return True

    def store_offers(self, depart_date: str, return_date: str, offers: list,
                     uid: str = OWNER):
        """Полная запись всех офферов (используется владельцем/тестами)."""
        now = datetime.utcnow().isoformat(timespec="seconds")
        rows = [
            (now, depart_date, return_date, int(o["price"]),
             o.get("airline"),
             _int_or_none(o.get("transfers")),
             _int_or_none(o.get("return_transfers")),
             _int_or_none(o.get("duration_to")),
             _int_or_none(o.get("duration_back")),
             o.get("departure_at"), o.get("return_at"), o.get("link"), uid)
            for o in offers
        ]
        self.conn.executemany(
            """INSERT INTO observations
               (ts, depart_date, return_date, price, airline, transfers,
                return_transfers, duration_to_min, duration_back_min,
                departure_at, return_at, link, uid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        self.conn.commit()

    def distinct_days(self, depart_date: str, return_date: str, days: int,
                      uid: str = OWNER) -> int:
        cur = self.conn.execute(
            """SELECT COUNT(DISTINCT date(ts)) FROM observations
               WHERE uid=? AND depart_date=? AND return_date=?
                 AND ts >= datetime('now', ?)""",
            (uid, depart_date, return_date, f"-{days} days"))
        return cur.fetchone()[0]

    def history_min(self, depart_date: str, return_date: str, days: int,
                    uid: str = OWNER):
        cur = self.conn.execute(
            """SELECT MIN(price) FROM observations
               WHERE uid=? AND depart_date=? AND return_date=?
                 AND ts >= datetime('now', ?)""",
            (uid, depart_date, return_date, f"-{days} days"))
        v = cur.fetchone()[0]
        return int(v) if v is not None else None

    def daily_min_series(self, depart_date: str, return_date: str, days: int,
                         uid: str = OWNER):
        cur = self.conn.execute(
            """SELECT date(ts) AS d, MIN(price) FROM observations
               WHERE uid=? AND depart_date=? AND return_date=?
                 AND ts >= datetime('now', ?)
               GROUP BY d ORDER BY d""",
            (uid, depart_date, return_date, f"-{days} days"))
        return [(r[0], int(r[1])) for r in cur.fetchall()]

    def latest_offers_today(self, depart_date: str, return_date: str,
                            uid: str = OWNER):
        cur = self.conn.execute(
            """SELECT * FROM observations
               WHERE uid=? AND depart_date=? AND return_date=?
                 AND date(ts) = date('now')
                 AND ts = (SELECT MAX(ts) FROM observations
                           WHERE uid=? AND depart_date=? AND return_date=?
                             AND date(ts) = date('now'))""",
            (uid, depart_date, return_date, uid, depart_date, return_date))
        return [dict(r) for r in cur.fetchall()]

    def latest_best_all(self, uid: str, hours: int = 48) -> list[dict]:
        """Лучший оффер по каждой паре за последние N часов (для дайджеста)."""
        cur = self.conn.execute(
            """SELECT o.* FROM observations o
               JOIN (SELECT depart_date, return_date, MAX(ts) AS mt
                     FROM observations
                     WHERE uid=? AND ts >= datetime('now', ?)
                     GROUP BY depart_date, return_date) x
                 ON o.depart_date=x.depart_date
                AND o.return_date=x.return_date
                AND o.ts=x.mt
               WHERE o.uid=?""",
            (uid, f"-{hours} hours", uid))
        return [dict(r) for r in cur.fetchall()]

    def purge(self, retention_days: int = 45):
        """Удаление старых наблюдений + еженедельный VACUUM."""
        self.conn.execute(
            "DELETE FROM observations WHERE ts < datetime('now', ?)",
            (f"-{int(retention_days)} days",))
        self.conn.execute(
            "DELETE FROM alerts WHERE ts < datetime('now', '-90 days')")
        self.conn.commit()
        last = self.get_meta("last_vacuum", "")
        if last != datetime.utcnow().date().isoformat()[:7]:  # раз в месяц
            self.conn.execute("VACUUM")
            self.set_meta("last_vacuum",
                          datetime.utcnow().date().isoformat()[:7])
            log.info("VACUUM выполнен")

    # ---------------- alerts ----------------

    def recent_alert(self, kind: str, depart_date: str, return_date: str,
                     hours: int, uid: str = OWNER):
        cur = self.conn.execute(
            """SELECT price, ts FROM alerts
               WHERE uid=? AND kind=? AND depart_date=? AND return_date=?
                 AND ts >= datetime('now', ?)
               ORDER BY ts DESC LIMIT 1""",
            (uid, kind, depart_date, return_date, f"-{hours} hours"))
        row = cur.fetchone()
        return {"price": row[0], "ts": row[1]} if row else None

    def record_alert(self, kind: str, depart_date: str, return_date: str,
                     price, link, uid: str = OWNER):
        self.conn.execute(
            """INSERT INTO alerts (ts, kind, depart_date, return_date, price,
                 link, uid) VALUES (datetime('now'),?,?,?,?,?,?)""",
            (kind, depart_date, return_date, price, link, uid))
        self.conn.commit()

    # ---------------- users ----------------

    def upsert_user(self, uid: str, ident_enc: str, joined: str) -> bool:
        """True — создан новый пользователь, False — уже существовал."""
        cur = self.conn.execute("SELECT uid FROM users WHERE uid=?", (uid,))
        if cur.fetchone():
            return False
        self.conn.execute(
            """INSERT INTO users (uid, ident_enc, status, joined)
               VALUES (?,?,'active',?)""", (uid, ident_enc, joined))
        self.conn.commit()
        return True

    def get_user(self, uid: str):
        cur = self.conn.execute("SELECT * FROM users WHERE uid=?", (uid,))
        return cur.fetchone()

    def all_users(self, status: str | None = None, with_tracking=False,
                  waitlist=None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM users WHERE 1=1", []
        if status:
            q += " AND status=?"; args.append(status)
        if waitlist is not None:
            q += " AND waitlist=?"; args.append(int(waitlist))
        if with_tracking:
            q += " AND tracking_enc IS NOT NULL"
        q += " ORDER BY joined, uid"
        return self.conn.execute(q, args).fetchall()

    def update_user(self, uid: str, **fields):
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE users SET {sets} WHERE uid=?",
                          (*fields.values(), uid))
        self.conn.commit()

    def delete_user(self, uid: str):
        self.conn.execute("DELETE FROM users WHERE uid=?", (uid,))
        self.conn.commit()

    def active_tracking_count(self, exclude_uid: str | None = None) -> int:
        q = """SELECT COUNT(*) FROM users
               WHERE status='active' AND tracking_enc IS NOT NULL"""
        args = []
        if exclude_uid:
            q += " AND uid<>?"; args.append(exclude_uid)
        return self.conn.execute(q, args).fetchone()[0]

    def oldest_waitlist(self):
        cur = self.conn.execute(
            """SELECT * FROM users
               WHERE status='active' AND waitlist=1 AND wl_notified=0
               ORDER BY joined, uid LIMIT 1""")
        return cur.fetchone()


def _int_or_none(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
