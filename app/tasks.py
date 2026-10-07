"""Task register (v0.5): what Sir Muhammad Ali asked, of whom, by when, and what happened.

SQLite file in STATE_DIR (kept on the HP server in ./state), so tasks survive restarts.
Every change is also written as an event, so the history of a task is always on record.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL DEFAULT 'open',          -- open | done | cancelled
  title TEXT NOT NULL,
  client TEXT,
  message TEXT NOT NULL,
  assignee_id TEXT NOT NULL,
  assignee_name TEXT,
  delivery TEXT NOT NULL DEFAULT 'private',     -- private | channel
  channel_id INTEGER,                           -- where the task was posted
  post_id INTEGER,                              -- ACE's task message (replies come in its thread)
  due TEXT,                                     -- YYYY-MM-DD
  follow_mode TEXT NOT NULL DEFAULT 'default',  -- default | daily | hours | none
  follow_time TEXT,                             -- HH:MM for daily
  follow_every REAL,                            -- hours
  next_remind_at TEXT,
  reminders INTEGER NOT NULL DEFAULT 0,
  escalate_at TEXT,
  escalated INTEGER NOT NULL DEFAULT 0,
  overdue_notified INTEGER NOT NULL DEFAULT 0,
  paused INTEGER NOT NULL DEFAULT 0,
  ext_due TEXT, ext_reason TEXT,                -- extension waiting for approval
  last_update TEXT,
  created_by TEXT, created_at TEXT, sent_at TEXT, closed_at TEXT, close_note TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, ts TEXT, kind TEXT, by TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def tz() -> ZoneInfo:
    return ZoneInfo(settings.timezone)


def now() -> datetime:
    return datetime.now(tz())


def ref(task_id: int) -> str:
    return f"T-{task_id:04d}"


def parse_ref(text: str) -> int | None:
    import re
    m = re.search(r"\bT-?0*(\d{1,6})\b", text or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


# --- working hours ------------------------------------------------------------------
@dataclass
class Hours:
    days: set[int] = field(default_factory=lambda: {0, 1, 2, 3, 4})   # Mon-Fri
    start: time = time(9, 0)
    end: time = time(18, 0)

    @classmethod
    def from_settings(cls) -> "Hours":
        return cls(days=set(settings.work_days), start=settings.work_start, end=settings.work_end)

    def is_open(self, t: datetime) -> bool:
        return t.weekday() in self.days and self.start <= t.time() < self.end

    def next_open(self, t: datetime) -> datetime:
        """t itself if within hours, else the next opening time."""
        if self.is_open(t):
            return t
        d = t
        if d.weekday() in self.days and d.time() < self.start:
            return d.replace(hour=self.start.hour, minute=self.start.minute, second=0, microsecond=0)
        for _ in range(14):
            d = (d + timedelta(days=1)).replace(hour=self.start.hour, minute=self.start.minute,
                                                 second=0, microsecond=0)
            if d.weekday() in self.days:
                return d
        return t

    def next_at(self, t: datetime, at: time) -> datetime:
        """Next working day moment at `at` strictly after t."""
        d = t.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if d <= t:
            d += timedelta(days=1)
        for _ in range(14):
            if d.weekday() in self.days:
                return d
            d += timedelta(days=1)
        return d


def next_reminder(task: dict, after: datetime, hours: Hours) -> datetime | None:
    """When the next follow-up for a task is due."""
    mode = task.get("follow_mode") or "default"
    if mode == "none" or task.get("status") != "open":
        return None
    if mode == "hours":
        every = float(task.get("follow_every") or 24)
        return hours.next_open(after + timedelta(hours=every))
    at = time.fromisoformat(task.get("follow_time") or "10:00")
    if mode == "daily":
        return hours.next_at(after, at)
    # default: on the due date (or the next working day without one), then daily
    due = task.get("due")
    if due:
        first = datetime.combine(date.fromisoformat(due), at, tz())
        if first > after:
            return first if first.weekday() in hours.days else hours.next_at(first, at)
    return hours.next_at(after, at)


# --- store ------------------------------------------------------------------------------
class TaskStore:
    def __init__(self, path=None) -> None:
        self.path = path or (settings.state_dir / "ace.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _row(self, r) -> dict | None:
        return dict(r) if r else None

    def add(self, **fields) -> dict:
        fields.setdefault("created_at", now().isoformat(timespec="seconds"))
        cols = ", ".join(fields)
        with self._lock:
            cur = self.db.execute(f"INSERT INTO tasks ({cols}) VALUES ({', '.join('?' * len(fields))})",
                                  list(fields.values()))
            self.db.commit()
            tid = cur.lastrowid
        self.event(tid, "created", fields.get("created_by"), fields.get("title"))
        return self.get(tid)

    def get(self, tid: int) -> dict | None:
        with self._lock:
            return self._row(self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())

    def update(self, tid: int, **fields) -> dict:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            with self._lock:
                self.db.execute(f"UPDATE tasks SET {sets} WHERE id=?", [*fields.values(), tid])
                self.db.commit()
        return self.get(tid)

    def find(self, where: str = "1=1", args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY id", args)]

    def open_tasks(self, assignee_id: str | None = None) -> list[dict]:
        if assignee_id is None:
            return self.find("status='open'")
        return self.find("status='open' AND assignee_id=?", (str(assignee_id),))

    def by_post(self, post_id: int) -> dict | None:
        rows = self.find("post_id=?", (post_id,))
        return rows[0] if rows else None

    def event(self, tid, kind: str, by, text: str = "") -> None:
        with self._lock:
            self.db.execute("INSERT INTO events (task_id, ts, kind, by, text) VALUES (?,?,?,?,?)",
                            (tid, now().isoformat(timespec="seconds"), kind, str(by or ""), text or ""))
            self.db.commit()

    def events(self, tid: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE task_id=? ORDER BY id", (tid,))]

    def events_since(self, since: datetime, kinds: tuple[str, ...]) -> list[dict]:
        q = f"SELECT * FROM events WHERE ts>=? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id"
        with self._lock:
            return [dict(r) for r in self.db.execute(q, (since.isoformat(timespec="seconds"), *kinds))]

    def get_meta(self, key: str, default=None):
        with self._lock:
            r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, json.dumps(value)))
            self.db.commit()
