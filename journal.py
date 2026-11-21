"""
Local durable journal (SQLite) - the store-and-forward buffer.

Flow of every record:
    device log --(poll)--> journal [synced=0] --(insert + read-back)--> Oracle
                                    [synced=1]

* A record is written (and fsync'd) here before anything is sent to Oracle,
  so a crash, power cut or Oracle outage cannot lose it.
* A record is only marked synced after it was READ BACK from Oracle.
* Records are never deleted from the journal.
* The primary key matches Oracle's UNIQUE (USERID, CHECKTIME, DEVICEID).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta

TS_FMT = "%Y-%m-%d %H:%M:%S"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS punches (
    device_id     INTEGER NOT NULL,
    user_id       TEXT    NOT NULL,
    checktime     TEXT    NOT NULL,          -- 'YYYY-MM-DD HH:MM:SS' (device local time)
    status        INTEGER,                    -- verify method
    punch         INTEGER,                    -- punch state
    first_seen    TEXT    NOT NULL,
    synced        INTEGER NOT NULL DEFAULT 0,
    synced_at     TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT    NOT NULL DEFAULT '',
    last_error    TEXT,
    PRIMARY KEY (device_id, user_id, checktime)
);
CREATE INDEX IF NOT EXISTS ix_punches_pending
    ON punches (next_retry_at, attempts) WHERE synced = 0;
CREATE INDEX IF NOT EXISTS ix_punches_dev_time
    ON punches (device_id, checktime);
CREATE INDEX IF NOT EXISTS ix_punches_time
    ON punches (checktime);

CREATE TABLE IF NOT EXISTS bad_records (
    device_id  INTEGER NOT NULL,
    raw_hex    TEXT    NOT NULL,
    error      TEXT,
    first_seen TEXT    NOT NULL,
    PRIMARY KEY (device_id, raw_hex)
);

-- Last known record count per device, to detect logs deleted by other software.
CREATE TABLE IF NOT EXISTS device_state (
    device_id      INTEGER PRIMARY KEY,
    last_count     INTEGER,
    last_read_at   TEXT,      -- last successful full read
    cleared_at     TEXT,      -- last time the device log shrank
    cleared_from   INTEGER,
    cleared_to     INTEGER,
    cleared_after  TEXT       -- last successful read before it shrank
);

CREATE TABLE IF NOT EXISTS devices_cache (
    device_id INTEGER PRIMARY KEY,
    ip        TEXT    NOT NULL,
    port      INTEGER NOT NULL
);
"""


def now_str() -> str:
    return datetime.now().strftime(TS_FMT)


def ts_to_str(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def str_to_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


class Journal:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        conn = self._conn()
        conn.executescript(_SCHEMA)

    # One connection per thread (sqlite3 connections are not shared).
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")   # fsync on every commit
            conn.execute("PRAGMA busy_timeout=60000")
            self._local.conn = conn
        return conn

    def _tx(self):
        return _Tx(self._conn())

    # ── Device -> journal ─────────────────────────────────────────────────────
    def add_records(self, device_id: int, records) -> int:
        """Insert new records (existing keys ignored). Returns number added."""
        if not records:
            return 0
        seen = now_str()
        rows = [(device_id, r.user_id, ts_to_str(r.timestamp), r.status, r.punch, seen)
                for r in records]
        with self._tx() as c:
            before = c.total_changes
            c.executemany(
                "INSERT OR IGNORE INTO punches "
                "(device_id, user_id, checktime, status, punch, first_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
            return c.total_changes - before

    def add_bad(self, device_id: int, bad) -> list:
        """Store undecodable records. Returns the ones not seen before."""
        new = []
        if not bad:
            return new
        seen = now_str()
        with self._tx() as c:
            for b in bad:
                cur = c.execute(
                    "INSERT OR IGNORE INTO bad_records (device_id, raw_hex, error, first_seen) "
                    "VALUES (?, ?, ?, ?)", (device_id, b.raw_hex, b.error, seen))
                if cur.rowcount:
                    new.append(b)
        return new

    def count_for_device(self, device_id: int) -> int:
        return self._conn().execute(
            "SELECT COUNT(*) FROM punches WHERE device_id = ?", (device_id,)).fetchone()[0]

    # ── Journal -> Oracle ─────────────────────────────────────────────────────
    def fetch_pending(self, limit: int) -> list[tuple]:
        """(device_id, user_id, checktime, status, punch, attempts) ready to send."""
        return self._conn().execute(
            "SELECT device_id, user_id, checktime, status, punch, attempts FROM punches "
            "WHERE synced = 0 AND next_retry_at <= ? "
            "ORDER BY attempts, device_id, checktime LIMIT ?",
            (now_str(), limit)).fetchall()

    def mark_synced(self, keys) -> None:
        if not keys:
            return
        at = now_str()
        with self._tx() as c:
            c.executemany(
                "UPDATE punches SET synced = 1, synced_at = ?, last_error = NULL "
                "WHERE device_id = ? AND user_id = ? AND checktime = ?",
                [(at,) + tuple(k) for k in keys])

    def mark_failed(self, failures: dict) -> None:
        """failures: {(device_id, user_id, checktime): (attempts_so_far, error)}"""
        if not failures:
            return
        now = datetime.now()
        rows = []
        for key, (attempts, error) in failures.items():
            delay = min(5 * (2 ** min(attempts, 10)), 3600)   # 5s .. 1h back-off
            rows.append((ts_to_str(now + timedelta(seconds=delay)), str(error)[:2000]) + tuple(key))
        with self._tx() as c:
            c.executemany(
                "UPDATE punches SET attempts = attempts + 1, next_retry_at = ?, last_error = ? "
                "WHERE device_id = ? AND user_id = ? AND checktime = ?", rows)

    def mark_unsynced(self, keys) -> None:
        """Audit found these missing in Oracle: send them again immediately."""
        if not keys:
            return
        with self._tx() as c:
            c.executemany(
                "UPDATE punches SET synced = 0, synced_at = NULL, next_retry_at = '', "
                "last_error = 'missing in Oracle (audit)' "
                "WHERE device_id = ? AND user_id = ? AND checktime = ?",
                [tuple(k) for k in keys])

    # ── Audit / stats ─────────────────────────────────────────────────────────
    def device_ids(self) -> list[int]:
        return [r[0] for r in self._conn().execute("SELECT DISTINCT device_id FROM punches")]

    def synced_keys_since(self, device_id: int, since: str) -> set:
        return {(r[0], r[1]) for r in self._conn().execute(
            "SELECT user_id, checktime FROM punches "
            "WHERE device_id = ? AND synced = 1 AND checktime >= ?", (device_id, since))}

    def stats(self) -> dict:
        out = {}
        for dev, total, pending, failing in self._conn().execute(
                "SELECT device_id, COUNT(*), SUM(synced = 0), SUM(synced = 0 AND attempts > 0) "
                "FROM punches GROUP BY device_id"):
            out[dev] = {"total": total, "pending": pending or 0, "failing": failing or 0}
        return out

    def failing_samples(self, limit: int = 5) -> list[tuple]:
        return self._conn().execute(
            "SELECT device_id, user_id, checktime, attempts, last_error FROM punches "
            "WHERE synced = 0 AND attempts > 0 ORDER BY attempts DESC LIMIT ?",
            (limit,)).fetchall()

    def bad_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM bad_records").fetchone()[0]

    # ── Device record-count tracking (detects logs deleted by other software) ──
    def device_state(self, device_id: int) -> dict | None:
        row = self._conn().execute(
            "SELECT last_count, last_read_at, cleared_at, cleared_from, cleared_to, cleared_after "
            "FROM device_state WHERE device_id = ?", (device_id,)).fetchone()
        if row is None:
            return None
        keys = ("last_count", "last_read_at", "cleared_at", "cleared_from", "cleared_to", "cleared_after")
        return dict(zip(keys, row))

    def all_device_states(self) -> dict:
        out = {}
        for row in self._conn().execute(
                "SELECT device_id, last_count, last_read_at, cleared_at, cleared_from, cleared_to, "
                "cleared_after FROM device_state"):
            out[row[0]] = dict(zip(("last_count", "last_read_at", "cleared_at", "cleared_from",
                                    "cleared_to", "cleared_after"), row[1:]))
        return out

    def set_device_count(self, device_id: int, count: int) -> None:
        """Called after every successful full read of the device log."""
        with self._tx() as c:
            c.execute(
                "INSERT INTO device_state (device_id, last_count, last_read_at) VALUES (?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET last_count = excluded.last_count, "
                "last_read_at = excluded.last_read_at",
                (device_id, count, now_str()))

    def record_log_reduction(self, device_id: int, old: int, new: int, last_read_at) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE device_state SET last_count = ?, cleared_at = ?, cleared_from = ?, "
                "cleared_to = ?, cleared_after = ? WHERE device_id = ?",
                (new, now_str(), old, new, last_read_at, device_id))

    # ── Device list cache (so collection works while Oracle is down) ──────────
    def cache_devices(self, devices: list[dict]) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM devices_cache")
            c.executemany("INSERT INTO devices_cache (device_id, ip, port) VALUES (?, ?, ?)",
                          [(d["device_id"], d["ip"], d["port"]) for d in devices])

    def cached_devices(self) -> list[dict]:
        return [{"device_id": r[0], "ip": r[1], "port": r[2]} for r in
                self._conn().execute("SELECT device_id, ip, port FROM devices_cache")]


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT / ROLLBACK context manager."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
