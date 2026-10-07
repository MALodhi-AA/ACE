"""What the attendance + task bot knows, read-only (v0.6).

Tables used (attendance_db on the DS723+):
  employees          id, name, chat_username, synology_user_ref, timezone, shift_start, grace_minutes, is_active
  attendance_logs    employee_id, attendance_date (local date), day_type present/absent/leave,
                     check_in, check_out (UTC), status working/on_break/auto_break/checked_out
  tasks              the bot's tasks (task_code, title, status, assigned_to_employee_id, assigned_due_at_utc,
                     eta_status, extension_status, creation_approval_status, completed_at_utc ...)
  chat_delivery_failures   messages the bot could not deliver (only counted and grouped by error)

Times in the bot's database are UTC (columns without _utc too, e.g. check_in).
Attendance details are for Sir Muhammad Ali only - never shown to staff.
"""
from __future__ import annotations

import logging
import os
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from integrations.attendance.db import AttendanceDB, configured

log = logging.getLogger(__name__)
UTC = timezone.utc
ACTIVE_TASK = ("assigned", "eta_proposed", "eta_approved", "in_progress", "blocked", "submitted", "overdue", "reopened")


def _local_tz() -> ZoneInfo:
    return ZoneInfo(os.getenv("TZ", "Asia/Dubai"))


def _utc(dt) -> datetime | None:
    if not dt:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


@dataclass
class Person:
    employee_id: int
    name: str
    chat_username: str
    chat_ref: str
    tz: ZoneInfo
    shift_start: time | None
    grace: int
    reports_to: int | None = None


@dataclass
class Day:
    person: Person
    day_type: str          # present | absent | leave | none (no record yet)
    check_in: datetime | None
    check_out: datetime | None
    status: str            # working | on_break | auto_break | checked_out | ''
    leave_reason: str = ""

    @property
    def in_office(self) -> bool:
        """Checked in today and not checked out (breaks count as in)."""
        return bool(self.check_in) and not self.check_out and self.status != "checked_out"

    @property
    def late_minutes(self) -> int:
        if not self.check_in or not self.person.shift_start:
            return 0
        local_in = self.check_in.astimezone(self.person.tz)
        start = datetime.combine(local_in.date(), self.person.shift_start, self.person.tz)
        minutes = int((local_in - start).total_seconds() // 60)
        return minutes if minutes > self.person.grace else 0     # late = after shift start, beyond the grace


class Bot:
    """Read-only view of the attendance + task bot."""

    def __init__(self, db: AttendanceDB | None = None, cache_seconds: int = 60) -> None:
        self.db = db or AttendanceDB()
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, object]] = {}
        self.last_error = ""

    @staticmethod
    def available() -> bool:
        return configured()

    def _cached(self, key: str, fn):
        hit = self._cache.get(key)
        if hit and _time.monotonic() - hit[0] < self.cache_seconds:
            return hit[1]
        try:
            value = fn()
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            log.warning("attendance DB: %s", exc)
            if hit:
                return hit[1]
            raise
        self._cache[key] = (_time.monotonic(), value)
        return value

    # ---------------------------------------------------------------- people
    def people(self) -> dict[int, Person]:
        def load():
            rows = self.db.query("SELECT id, name, chat_username, synology_user_ref, timezone, shift_start, "
                                 "grace_minutes, reports_to_employee_id FROM employees WHERE is_active=1")
            out = {}
            for r in rows:
                try:
                    tz = ZoneInfo(r["timezone"]) if r.get("timezone") else _local_tz()
                except Exception:  # noqa: BLE001
                    tz = _local_tz()
                ss = r.get("shift_start")
                if isinstance(ss, timedelta):            # MySQL TIME comes back as timedelta
                    ss = (datetime.min + ss).time()
                out[int(r["id"])] = Person(int(r["id"]), r.get("name") or "", (r.get("chat_username") or "").strip(),
                                           str(r.get("synology_user_ref") or "").strip(), tz, ss,
                                           int(r.get("grace_minutes") or 0), r.get("reports_to_employee_id"))
            return out
        return self._cached("people", load)

    def person_for_chat(self, chat_uid, chat_username: str = "") -> Person | None:
        uid, uname = str(chat_uid), (chat_username or "").strip().lower()
        for p in self.people().values():
            if p.chat_ref and p.chat_ref == uid:
                return p
        for p in self.people().values():
            if uname and uname in {p.chat_username.lower(), p.name.lower(), p.chat_ref.lower()}:
                return p
        return None

    # ---------------------------------------------------------------- attendance
    def today(self) -> list[Day]:
        return self.day(datetime.now(_local_tz()).date())

    def day(self, on) -> list[Day]:
        """Attendance of every active person on a date (local)."""
        def load():
            people = self.people()
            rows = self.db.query("SELECT employee_id, day_type, leave_reason, check_in, check_out, status "
                                 "FROM attendance_logs WHERE attendance_date=%s", (on,))
            by_emp = {int(r["employee_id"]): r for r in rows}
            days = []
            for eid, p in people.items():
                r = by_emp.get(eid)
                if not r:
                    days.append(Day(p, "none", None, None, ""))
                    continue
                days.append(Day(p, r.get("day_type") or "present", _utc(r.get("check_in")),
                                _utc(r.get("check_out")), r.get("status") or "", r.get("leave_reason") or ""))
            return days
        return self._cached(f"day:{on}", load)

    def day_for(self, person: Person) -> Day | None:
        return next((d for d in self.today() if d.person.employee_id == person.employee_id), None)

    def in_office(self, chat_uid, chat_username: str = "") -> bool | None:
        """True / False, or None if this person isn't in the attendance system."""
        p = self.person_for_chat(chat_uid, chat_username)
        if not p:
            return None
        d = self.day_for(p)
        return bool(d and d.in_office)

    # ---------------------------------------------------------------- the bot's tasks
    def tasks(self) -> dict:
        def load():
            now = datetime.now(UTC).replace(tzinfo=None)
            start_today = datetime.combine(datetime.now(_local_tz()).date(), time(0), _local_tz()) \
                .astimezone(UTC).replace(tzinfo=None)
            act = "','".join(ACTIVE_TASK)
            q = ("SELECT task_code, title, status, priority, assigned_to_employee_id AS emp, assigned_due_at_utc AS due, "
                 "eta_status, approved_eta_at_utc AS eta, extension_status, extension_requested_due_at_utc AS ext_due, "
                 "creation_approval_status AS cstat, completed_at_utc AS done_at, channel_name "
                 "FROM tasks WHERE is_active=1 AND ")
            open_rows = self.db.query(q + f"status IN ('{act}')")
            done_rows = self.db.query(q + "status='completed' AND completed_at_utc >= %s",
                                      (start_today - timedelta(days=1),))
            pending_create = self.db.query(q + "creation_approval_status='pending'")
            for r in open_rows + done_rows + pending_create:
                for k in ("due", "eta", "ext_due", "done_at"):
                    r[k] = _utc(r.get(k))
            nowu = now.replace(tzinfo=UTC)
            end_today = datetime.combine(datetime.now(_local_tz()).date(), time(23, 59), _local_tz())
            return {
                "open": open_rows,
                "overdue": [r for r in open_rows if r["status"] == "overdue" or (r["due"] and r["due"] < nowu)],
                "due_today": [r for r in open_rows if r["due"] and nowu <= r["due"] <= end_today],
                "blocked": [r for r in open_rows if r["status"] == "blocked"],
                "eta_waiting": [r for r in open_rows if r["eta_status"] == "proposed"],
                "ext_waiting": [r for r in open_rows if r["extension_status"] == "pending"],
                "submitted": [r for r in open_rows if r["status"] == "submitted"],
                "create_waiting": pending_create,
                "completed": [r for r in done_rows if r["done_at"] and r["done_at"] >= (nowu - timedelta(days=1))],
            }
        return self._cached("tasks", load)

    def tasks_of(self, person: Person) -> list[dict]:
        return [r for r in self.tasks()["open"] if int(r["emp"] or 0) == person.employee_id]

    # ---------------------------------------------------------------- delivery failures
    def delivery_failures(self) -> dict:
        def load():
            now = datetime.now(UTC).replace(tzinfo=None)
            hour = self.db.query("SELECT COUNT(*) AS n FROM chat_delivery_failures WHERE created_at_utc >= %s",
                                 (now - timedelta(hours=1),))[0]["n"]
            day = self.db.query("SELECT COUNT(*) AS n FROM chat_delivery_failures WHERE created_at_utc >= %s",
                                (now - timedelta(days=1),))[0]["n"]
            top = self.db.query("SELECT LEFT(error, 160) AS error, source, COUNT(*) AS n, MAX(created_at_utc) AS last "
                                "FROM chat_delivery_failures WHERE created_at_utc >= %s "
                                "GROUP BY LEFT(error, 160), source ORDER BY n DESC LIMIT 5",
                                (now - timedelta(hours=1),))
            chans = self.db.query("SELECT channel, COUNT(*) AS n FROM chat_delivery_failures WHERE created_at_utc >= %s "
                                  "GROUP BY channel ORDER BY n DESC LIMIT 5", (now - timedelta(hours=1),))
            return {"hour": hour, "day": day, "top": top, "channels": chans}
        return self._cached("failures", load)


def fmt_local(dt: datetime | None, tz: ZoneInfo | None = None) -> str:
    if not dt:
        return "-"
    return dt.astimezone(tz or _local_tz()).strftime("%H:%M")


def fmt_due(dt: datetime | None) -> str:
    if not dt:
        return "no due date"
    return dt.astimezone(_local_tz()).strftime("%a %d-%b %H:%M")
