"""Хранилище состояния и журнала событий (SQLite)."""
from __future__ import annotations

import sqlite3
import threading

from util import now_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    target_id     TEXT PRIMARY KEY,
    status        TEXT NOT NULL,
    since_ts      INTEGER NOT NULL,
    last_check_ts INTEGER,
    last_result   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    duration    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_target_ts ON events(target_id, ts);

CREATE TABLE IF NOT EXISTS flaps (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    seconds   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flaps_target_ts ON flaps(target_id, ts);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Storage:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------- состояние ----------

    def get_state(self, target_id: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM state WHERE target_id = ?", (target_id,)
            )
            return cur.fetchone()

    def touch(self, target_id: str, result: str) -> None:
        """Отметить факт проверки, не меняя статус."""
        with self._lock:
            self._conn.execute(
                "UPDATE state SET last_check_ts = ?, last_result = ? WHERE target_id = ?",
                (now_ts(), result, target_id),
            )
            self._conn.commit()

    def set_status(self, target_id: str, status: str, ts: int | None = None) -> int | None:
        """Записать новый статус. Возвращает длительность предыдущего, сек.

        ts позволяет датировать смену моментом первого несовпадения,
        а не моментом её подтверждения.
        """
        ts = ts or now_ts()
        previous = self.get_state(target_id)
        duration = ts - previous["since_ts"] if previous else None

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO state (target_id, status, since_ts, last_check_ts, last_result)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    status = excluded.status,
                    since_ts = excluded.since_ts,
                    last_check_ts = excluded.last_check_ts,
                    last_result = excluded.last_result
                """,
                (target_id, status, ts, now_ts(), status),
            )
            self._conn.execute(
                "INSERT INTO events (target_id, status, ts, duration) VALUES (?, ?, ?, ?)",
                (target_id, status, ts, duration),
            )
            self._conn.commit()
        return duration

    # ---------- журнал ----------

    def last_events(self, limit: int = 15, target_id: str | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM events"
        params: list = []
        if target_id:
            query += " WHERE target_id = ?"
            params.append(target_id)
        query += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return self._conn.execute(query, params).fetchall()

    # ---------- кратковременные сбои ----------

    def record_flap(self, target_id: str, seconds: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO flaps (target_id, ts, seconds) VALUES (?, ?, ?)",
                (target_id, now_ts(), seconds),
            )
            self._conn.commit()

    def flaps_count(self, target_id: str, since_ts: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM flaps WHERE target_id = ? AND ts >= ?",
                (target_id, since_ts),
            ).fetchone()
        return row["n"] if row else 0

    def last_flaps(self, limit: int = 15) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM flaps ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    def stats(self, target_id: str, since_ts: int) -> dict:
        """Количество отключений и суммарное время без света за период."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, ts, duration FROM events "
                "WHERE target_id = ? AND ts >= ? ORDER BY ts",
                (target_id, since_ts),
            ).fetchall()
            current = self._conn.execute(
                "SELECT status, since_ts FROM state WHERE target_id = ?", (target_id,)
            ).fetchone()

        outages = sum(1 for r in rows if r["status"] == "down")
        # Длительность записана в событии, которое закрывает предыдущий период.
        downtime = sum(
            r["duration"] or 0 for r in rows if r["status"] == "up" and r["duration"]
        )
        if current and current["status"] == "down":
            downtime += now_ts() - max(current["since_ts"], since_ts)
        return {
            "outages": outages,
            "downtime": downtime,
            "flaps": self.flaps_count(target_id, since_ts),
        }

    # ---------- настройки ----------

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()
