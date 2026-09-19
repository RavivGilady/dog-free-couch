"""
Event store: SQLite.

The original CSV log was fine for tailing in a terminal, but the dashboard
needs to page, filter and join events to their media, and needs a stable id
per event so a clip recorded *after* the "entered" row can be attached to
it. SQLite gives all of that with no server to run.

The CSV log is still written alongside this (see alert.log_event) so
nothing that already depends on it breaks.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT    NOT NULL,          -- 'entered' | 'left'
    start_ts    REAL    NOT NULL,
    end_ts      REAL,
    duration    REAL,
    confidence  REAL,
    snapshot    TEXT,
    video       TEXT,
    notified    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events (start_ts DESC);
"""


class EventStore:
    def __init__(self, db_path: str = "data/events.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The monitor thread and Flask request threads both touch this, so
        # serialise writes ourselves and allow cross-thread use.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def add_entered(self, start_ts, confidence=None, snapshot=None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (type, start_ts, confidence, snapshot) VALUES (?,?,?,?)",
                ("entered", start_ts, confidence, snapshot),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def add_left(self, start_ts, end_ts, duration) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events (type, start_ts, end_ts, duration) VALUES (?,?,?,?)",
                ("left", start_ts, end_ts, duration),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def attach_video(self, event_id: int, video_path: str) -> None:
        """Clips finish well after the 'entered' row is written, so the path
        is filled in once recording stops."""
        with self._lock:
            self._conn.execute("UPDATE events SET video=? WHERE id=?", (video_path, event_id))
            self._conn.commit()

    def mark_notified(self, event_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE events SET notified=1 WHERE id=?", (event_id,))
            self._conn.commit()

    def recent(self, limit: int = 100, offset: int = 0, only_alerts: bool = False):
        sql = "SELECT * FROM events"
        if only_alerts:
            sql += " WHERE type='entered'"
        sql += " ORDER BY start_ts DESC, id DESC LIMIT ? OFFSET ?"
        with self._lock:
            rows = self._conn.execute(sql, (limit, offset)).fetchall()
        return [dict(r) for r in rows]

    def get(self, event_id: int):
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def delete(self, event_id: int) -> dict | None:
        """Returns the deleted row so the caller can clean up its media."""
        row = self.get(event_id)
        if row:
            with self._lock:
                self._conn.execute("DELETE FROM events WHERE id=?", (event_id,))
                self._conn.commit()
        return row

    def stats(self) -> dict:
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) c FROM events WHERE type='entered'").fetchone()["c"]
            today = self._conn.execute(
                "SELECT COUNT(*) c FROM events WHERE type='entered' "
                "AND date(start_ts,'unixepoch','localtime') = date('now','localtime')"
            ).fetchone()["c"]
            total_time = self._conn.execute(
                "SELECT COALESCE(SUM(duration),0) s FROM events WHERE type='left'").fetchone()["s"]
            longest = self._conn.execute(
                "SELECT COALESCE(MAX(duration),0) m FROM events WHERE type='left'").fetchone()["m"]
        return {
            "total_alerts": total,
            "today_alerts": today,
            "total_seconds_on_couch": round(total_time or 0, 1),
            "longest_session_sec": round(longest or 0, 1),
        }

    def close(self):
        with self._lock:
            self._conn.close()
