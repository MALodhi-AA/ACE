"""v0.5: instructions from Sir Muhammad Ali, follow-ups, staff replies, digest."""
from datetime import datetime, time, timedelta

import pytest

import app.assistant as A
import app.tasks as T
from app.assistant import Assistant
from app.config import settings
from app.storage import LocalStore
from app.tasks import Hours, TaskStore
from integrations.synology_chat.watcher import ChannelWatcher
from tests.test_channel_watcher import BASE, CID, TalkChat, msg, post

MA, ALI = "7", "8"
DM_MA, DM_ALI = 300, 302
TZ = T.tz()


class Office(TalkChat):
    """ACE-TEST channel + direct chats with Sir Muhammad Ali (7) and Ali (8)."""

    def channels(self):
        chans = [c for c in super().channels() if c["channel_id"] != 301]
        chans.append({"channel_id": DM_ALI, "name": "", "type": "anonymous", "total_member_count": 2,
                      "is_joined": True, "last_post_at": self.last_post_at})
        return chans

    def users(self):
        return {7: "Muhammad Ali Lodhi", 8: "Ali", 9: "Amir Hussain"}

    def send(self, cid, text, thread_id=None):
        super().send(cid, text, thread_id)
        return (cid << 32) + 5000 + len(self.sent)


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


@pytest.fixture
def office(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ace_admins", [MA])
    monkeypatch.setattr(settings, "allowed_users", [])
    clock = Clock(datetime(2026, 10, 7, 11, 0, tzinfo=TZ))          # Wednesday 11:00
    monkeypatch.setattr(A, "now", clock)
    monkeypatch.setattr(T, "now", clock)
    chat = Office([post(1, "system"), msg(DM_MA, 1, "old", creator=7), msg(DM_ALI, 1, "old", creator=8)])
    chat.me = 189
    w = ChannelWatcher(chat, store=LocalStore(tmp_path / "f"), state_path=tmp_path / "s.json", tz="Asia/Dubai")
    replies = {}
    model = lambda system, user: replies["next"](system, user)  # noqa: E731
    w.assistant = Assistant(w, store=TaskStore(tmp_path / "ace.db"), ask_model=model)
    w.assistant.hours = Hours()                                        # Mon-Fri 09:00-18:00
    w.poll_once()
    n = {"DM_MA": 1, "DM_ALI": 1, "CID": 10}

    def say(who, text, where=None, thread=None, mentions=()):
        key = where or ("DM_MA" if who == MA else "DM_ALI")
        cid = {"DM_MA": DM_MA, "DM_ALI": DM_ALI, "CID": CID}[key]
        n[key] += 1
        m = msg(cid, n[key], text, creator=int(who), mentions=mentions)
        if thread:
            m["thread_id"] = thread
        chat.new(m)
        w.poll_once()
        w._pool.shutdown(wait=True)
        w._pool = type(w._pool)(max_workers=4)
        return [s for s in chat.sent]

    return chat, w, replies, clock, say


def parsed(**kw):
    base = {"is_instruction": True, "assignee": "Ali", "title": "Mara Q3 VAT working", "client": "Mara",
            "message": "Hi Ali, Sir Muhammad Ali has asked for the Mara Q3 VAT working by Fri 09-Oct-2026.",
            "due": "2026-10-09", "delivery": "private", "channel": None, "when": "now",
            "follow_up": {"mode": "daily", "time": "10:00", "every_hours": None},
            "escalate_at": None, "unclear": None}
    base.update(kw)
    return lambda s, u: base


def last_to(chat, cid):
    return [t for c, _, t in chat.sent if c == cid][-1]


def test_instruction_confirm_then_send_privately(office):
    chat, w, replies, clock, say = office
    say(ALI, "hi")                                 # Ali has chatted with ACE once
    replies["next"] = lambda s, u: {"kind": "other"}
    replies["next"] = parsed()
    say(MA, "ask Ali to send the Mara Q3 VAT working by Friday, follow up daily")
    confirm = last_to(chat, DM_MA)
    assert confirm.startswith("Please confirm, Sir Muhammad Ali:") and "To: Ali, privately" in confirm
    assert not [t for c, _, t in chat.sent if c == DM_ALI and "has asked" in t]     # nothing sent yet
    say(MA, "ok")
    assert "Sent. T-0001" in last_to(chat, DM_MA)
    to_ali = last_to(chat, DM_ALI)
    assert "Sir Muhammad Ali has asked" in to_ali and "(T-0001 - reply here with 'done'" in to_ali
    t = w.assistant.store.get(1)
    assert t["status"] == "open" and t["next_remind_at"].startswith("2026-10-08T10:00")


def test_correction_and_cancel(office):
    chat, w, replies, clock, say = office
    say(ALI, "hi")
    replies["next"] = parsed()
    say(MA, "tell Ali to send the Mara VAT working by Friday")
    replies["next"] = parsed(due="2026-10-12")
    say(MA, "make it Monday")
    assert "Due: Mon 12-Oct-2026" in last_to(chat, DM_MA)
    say(MA, "cancel")
    assert last_to(chat, DM_MA).startswith("Cancelled")
    assert w.assistant.store.open_tasks() == []


def test_unknown_person_and_no_direct_chat(office):
    chat, w, replies, clock, say = office
    replies["next"] = parsed(assignee="Zed")
    say(MA, "ask Zed to call the bank")
    assert "I don't know who 'Zed' is" in last_to(chat, DM_MA)
    replies["next"] = parsed(assignee="Amir Hussain")
    say(MA, "ask Amir to call the bank")
    text = last_to(chat, DM_MA)
    assert "To: Sir Amir Hussain, privately" in text and "can't message Sir Amir Hussain privately yet" in text


def send_task(office, **kw):
    chat, w, replies, clock, say = office
    say(ALI, "hi")
    replies["next"] = parsed(**kw)
    say(MA, "ask Ali to send the Mara Q3 VAT working by Friday")
    say(MA, "ok")
    return w.assistant.store.get(1)


def test_staff_done_closes_and_informs_manager(office):
    chat, w, replies, clock, say = office
    send_task(office)
    replies["next"] = lambda s, u: {"task": "T-0001", "kind": "done", "new_due": None, "summary": "sent to client"}
    say(ALI, "done, I've sent it")
    assert "marked T-0001 done" in last_to(chat, DM_ALI)
    assert last_to(chat, DM_MA).startswith("Done: T-0001 Mara Q3 VAT working - Ali")
    assert w.assistant.store.get(1)["status"] == "done"


def test_extension_request_and_approval(office):
    chat, w, replies, clock, say = office
    send_task(office)
    replies["next"] = lambda s, u: {"task": None, "kind": "extension", "new_due": "2026-10-12",
                                    "summary": "waiting for bank statements"}
    say(ALI, "can I have till Monday? bank statements not received")
    assert "Reply 'approve T-0001' or 'reject T-0001'" in last_to(chat, DM_MA)
    say(MA, "approve T-0001")
    assert "now due Mon 12-Oct-2026" in last_to(chat, DM_MA)
    assert "approved more time" in last_to(chat, DM_ALI)
    assert w.assistant.store.get(1)["due"] == "2026-10-12"


def test_message_for_manager_without_tasks(office):
    chat, w, replies, clock, say = office
    replies["next"] = lambda s, u: {"task": None, "kind": "for_manager", "summary": "client called"}
    say(MA, "hi")                                   # Sir Muhammad Ali's direct chat is known
    say(ALI, "please tell Sir Muhammad Ali the Mara client called")
    assert last_to(chat, DM_MA).startswith("Message from Ali: please tell")
    assert "passed your message" in last_to(chat, DM_ALI)


def test_reminders_overdue_and_digest(office):
    chat, w, replies, clock, say = office
    say(MA, "hi")
    send_task(office)                                # due Fri 09-Oct, daily 10:00
    clock.t = datetime(2026, 10, 8, 10, 1, tzinfo=TZ)
    w.assistant.tick(force=True)
    assert last_to(chat, DM_ALI).startswith("Reminder from ACE: T-0001 Mara Q3 VAT working")
    clock.t = datetime(2026, 10, 9, 18, 5, tzinfo=TZ)                    # Fri after hours
    w.assistant.tick(force=True)
    assert last_to(chat, DM_MA).startswith("Overdue: T-0001")
    clock.t = datetime(2026, 10, 10, 10, 1, tzinfo=TZ)                   # Saturday: no reminder
    before = len(chat.sent)
    w.assistant.tick(force=True)
    assert len(chat.sent) == before
    clock.t = datetime(2026, 10, 12, 8, 50, tzinfo=TZ)                   # Monday 08:50: digest
    w.assistant.tick(force=True)
    d = last_to(chat, DM_MA)
    assert d.startswith("Good morning Sir Muhammad Ali") and "Overdue: 1" in d
    clock.t = datetime(2026, 10, 12, 10, 1, tzinfo=TZ)
    w.assistant.tick(force=True)
    assert "now overdue" in last_to(chat, DM_ALI)


def test_channel_task_and_reply_in_its_thread(office):
    chat, w, replies, clock, say = office
    replies["next"] = parsed(delivery="channel", channel="ACE-TEST")
    say(MA, "ask Ali in ACE-TEST to send the Mara VAT working by Friday")
    say(MA, "ok")
    t = w.assistant.store.get(1)
    assert t["delivery"] == "channel" and t["channel_id"] == CID and t["post_id"]
    replies["next"] = lambda s, u: {"task": None, "kind": "update", "summary": "half done"}
    say(ALI, "50% done, will finish tomorrow", where="CID", thread=t["post_id"])
    assert "noted on T-0001" in [x for c, th, x in chat.sent if c == CID][-1]


def test_staff_question_is_answered_normally(office):
    chat, w, replies, clock, say = office
    say(ALI, "whoami")                              # no open tasks -> not a task reply
    assert "user_id: 8" in last_to(chat, DM_ALI)


def test_only_manager_gives_instructions(office):
    chat, w, replies, clock, say = office
    send_task(office)
    replies["next"] = lambda s, u: {"kind": "other"}
    say(ALI, "ask Amir to do my work")
    assert not w.assistant.store.find("assignee_id='9'")


def test_hours_helpers():
    h = Hours()
    fri_eve = datetime(2026, 10, 9, 19, 0, tzinfo=TZ)
    assert h.next_open(fri_eve) == datetime(2026, 10, 12, 9, 0, tzinfo=TZ)
    assert h.next_at(fri_eve, time(10, 0)) == datetime(2026, 10, 12, 10, 0, tzinfo=TZ)
    t = {"status": "open", "follow_mode": "hours", "follow_every": 2}
    assert T.next_reminder(t, datetime(2026, 10, 7, 17, 0, tzinfo=TZ), h) == datetime(2026, 10, 8, 9, 0, tzinfo=TZ)
    t = {"status": "open", "follow_mode": "default", "due": "2026-10-09"}       # default: start of work
    assert T.next_reminder(t, datetime(2026, 10, 7, 11, 0, tzinfo=TZ), h) == datetime(2026, 10, 9, 9, 0, tzinfo=TZ)
    assert T.parse_ref("approve t-12") == 12
    assert timedelta(0) <= timedelta(0)


# --- v0.6: attendance + task bot (read-only) ------------------------------------------
from datetime import timezone  # noqa: E402

from integrations.attendance.reader import Bot  # noqa: E402

UTC = timezone.utc


class FakeAttendanceDB:
    """Answers the reader's SELECTs from in-memory rows (like attendance_db on the DS723+)."""

    def __init__(self):
        self.employees = [
            {"id": 1, "name": "Muhammad Ali Lodhi", "chat_username": "ma", "synology_user_ref": "7",
             "timezone": "Asia/Dubai", "shift_start": timedelta(hours=11), "grace_minutes": 10,
             "reports_to_employee_id": None},
            {"id": 2, "name": "Ali", "chat_username": "ali", "synology_user_ref": "8",
             "timezone": "Asia/Dubai", "shift_start": timedelta(hours=11), "grace_minutes": 10,
             "reports_to_employee_id": 1},
            {"id": 3, "name": "Amir Hussain", "chat_username": "amir", "synology_user_ref": "9",
             "timezone": "Asia/Karachi", "shift_start": timedelta(hours=9), "grace_minutes": 0,
             "reports_to_employee_id": 1},
        ]
        self.logs = {}           # employee_id -> row
        self.tasks = []
        self.failures = {"hour": 0, "day": 0}
        self.queries = []

    def query(self, sql, args=None):
        self.queries.append(sql)
        self.last_args = args
        assert sql.lstrip().upper().startswith("SELECT")
        if "FROM employees" in sql:
            return [dict(e) for e in self.employees]
        if "FROM attendance_logs" in sql:
            return [dict(r, employee_id=k) for k, r in self.logs.items()]
        if "FROM tasks" in sql:
            if "status='completed'" in sql:
                return [dict(t) for t in self.tasks if t["status"] == "completed"]
            if "creation_approval_status='pending'" in sql:
                return [dict(t) for t in self.tasks if t.get("cstat") == "pending"]
            return [dict(t) for t in self.tasks if t["status"] not in ("completed", "cancelled")]
        if "chat_delivery_failures" in sql:
            if "COUNT(*) AS n FROM" in sql and "GROUP BY" not in sql:
                return [{"n": self.failures["day" if "86400" in str(args) or len(self.queries) % 2 == 0 else "hour"]}]
            if "GROUP BY LEFT(error" in sql:
                return [{"error": "create post too fast", "source": "reminder", "n": 40000, "last": None}] \
                    if self.failures["hour"] else []
            return [{"channel": "Mara-Daily-Work", "n": 40000}] if self.failures["hour"] else []
        raise AssertionError(sql)


def bot_task(code, title, emp, status="in_progress", due=None, **kw):
    return {"task_code": code, "title": title, "status": status, "priority": "normal", "emp": emp, "due": due,
            "eta_status": kw.get("eta_status", "approved"), "eta": None,
            "extension_status": kw.get("extension_status", "none"), "ext_due": None,
            "cstat": kw.get("cstat", "approved"), "done_at": kw.get("done_at"), "channel_name": "x"}


@pytest.fixture
def office_att(office, monkeypatch):
    chat, w, replies, clock, say = office
    db = FakeAttendanceDB()
    bot = Bot(db=db, cache_seconds=0)
    w.assistant.bot = bot
    import integrations.attendance.reader as R

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.t.astimezone(tz) if tz else clock.t.replace(tzinfo=None)

    monkeypatch.setattr(R, "datetime", FixedDT)
    return chat, w, replies, clock, say, db


def check_in(db, emp, at_local):
    db.logs[emp] = {"day_type": "present", "leave_reason": None,
                    "check_in": at_local.astimezone(UTC).replace(tzinfo=None), "check_out": None, "status": "working"}


def test_send_when_checked_in(office_att):
    chat, w, replies, clock, say, db = office_att
    say(ALI, "hi")
    replies["next"] = parsed(when="checkin")
    say(MA, "ask Ali when he checks in to send the Mara VAT working by Friday")
    say(MA, "ok")
    assert "hasn't checked in yet - I'll send it as soon as they do" in last_to(chat, DM_MA)
    before = len([1 for c, _, _ in chat.sent if c == DM_ALI])
    clock.t = datetime(2026, 10, 7, 11, 30, tzinfo=TZ)
    w.assistant.tick(force=True)
    assert len([1 for c, _, _ in chat.sent if c == DM_ALI]) == before          # still not in
    check_in(db, 2, datetime(2026, 10, 7, 11, 40, tzinfo=TZ))
    clock.t = datetime(2026, 10, 7, 11, 41, tzinfo=TZ)
    w.assistant.tick(force=True)
    assert "Sir Muhammad Ali has asked" in last_to(chat, DM_ALI)


def test_reminders_only_while_checked_in(office_att):
    chat, w, replies, clock, say, db = office_att
    say(ALI, "hi")
    replies["next"] = parsed(follow_up={"mode": "hours", "every_hours": 1})
    say(MA, "ask Ali for the Mara VAT working, remind every hour")
    say(MA, "ok")
    n = lambda: len([1 for c, _, t in chat.sent if c == DM_ALI and t.startswith("Reminder")])  # noqa: E731
    clock.t = datetime(2026, 10, 7, 13, 0, tzinfo=TZ)
    w.assistant.tick(force=True)
    assert n() == 0                                          # Ali not checked in today
    check_in(db, 2, datetime(2026, 10, 7, 12, 55, tzinfo=TZ))
    w.assistant.tick(force=True)
    assert n() == 1


def test_attendance_and_team_tasks_in_digest_and_commands(office_att):
    chat, w, replies, clock, say, db = office_att
    say(MA, "hi")
    check_in(db, 1, datetime(2026, 10, 7, 10, 55, tzinfo=TZ))
    check_in(db, 2, datetime(2026, 10, 7, 11, 42, tzinfo=TZ))                 # 42 min after shift start (grace 10)
    db.logs[3] = {"day_type": "leave", "leave_reason": "sick", "check_in": None, "check_out": None, "status": ""}
    past = datetime(2026, 10, 5, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    db.tasks = [bot_task("T2610-001", "Mara Q3 VAT return", 2, status="overdue", due=past),
                bot_task("T2610-002", "Volt bank rec", 3, extension_status="pending"),
                bot_task("T2610-003", "Food Box MIS", 2, status="blocked"),
                bot_task("T2604-082", "VAT Filing", 1, status="submitted", due=past)]   # with the reviewer
    say(MA, "who is in")
    a = last_to(chat, DM_MA)
    assert "Attendance today: 2 of 3 checked in" in a and "Late: Ali (11:42, +42 min)" in a
    assert "On leave: Sir Amir Hussain (sick)" in a
    say(MA, "what is Ali working on?")
    p = last_to(chat, DM_MA)
    assert p.startswith("Ali today: checked in 11:42 (+42 min late)") and "T2610-001 Mara Q3 VAT return - overdue" in p
    db.failures = {"hour": 40000, "day": 900000}
    d = w.assistant.digest_text()
    assert "Team tasks (task bot): 4 open, 1 overdue" in d and "Extension requests waiting: 1" in d
    assert "Submitted, waiting for review: 1" in d                 # past due but not counted as overdue
    assert "Warning: the task bot failed to deliver" in d and "My follow-ups:" in d
    say(MA, "bot errors")
    assert "create post too fast" in last_to(chat, DM_MA)


def test_staff_cannot_see_attendance(office_att):
    chat, w, replies, clock, say, db = office_att
    replies["next"] = lambda s, u: {"kind": "other"}
    say(ALI, "who is in")
    assert "Attendance:" not in last_to(chat, DM_ALI)


def test_reader_never_writes():
    db = FakeAttendanceDB()
    bot = Bot(db=db, cache_seconds=0)
    bot.people(); bot.today(); bot.tasks(); bot.delivery_failures()
    assert all(q.lstrip().upper().startswith("SELECT") for q in db.queries)


# --- v0.7: the brain (ladder + learning) --------------------------------------------------
from types import SimpleNamespace as NS  # noqa: E402


@pytest.fixture
def brainy(office_att):
    chat, w, replies, clock, say, db = office_att
    calls = {"fast": [], "full": []}
    picks = {"next": None}
    fulls = {"script": []}

    def fast(system, user):
        calls["fast"].append(user)
        return picks["next"](user) if callable(picks["next"]) else picks["next"]

    def full(system, messages, tools):
        calls["full"].append(messages)
        return fulls["script"].pop(0)(messages)

    w.assistant._fast_brain, w.assistant._full_brain = fast, full
    w.run_later = lambda f: f()
    say(MA, "hi")
    check_in(db, 2, datetime(2026, 10, 7, 11, 5, tzinfo=TZ))
    db.tasks = [bot_task("T2610-009", "Mara payroll", 2), bot_task("T2610-010", "Volt VAT", 3, status="overdue")]
    return chat, w, say, db, calls, picks, fulls


def test_fast_ai_answers_and_learns_then_no_ai_next_time(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    picks["next"] = {"tool": "person_status", "args": {"name": "Ali"}, "complex": False}
    say(MA, "what is Ali doing these days?")
    assert last_to(chat, DM_MA).startswith("Ali today: checked in 11:05") and len(calls["fast"]) == 1
    pats = w.assistant.store.memories("pattern")
    assert pats[0]["key"] == "what is {person} doing these days" and pats[0]["value"]["args"] == {"name": "{person}"}
    say(MA, "What is Amir doing these days")                  # same phrasing, another person: no AI
    assert last_to(chat, DM_MA).startswith("Amir Hussain today:") is False      # title applied
    assert "Sir Amir Hussain today:" in last_to(chat, DM_MA) and len(calls["fast"]) == 1


def test_correction_forgets_pattern_and_uses_full_ai(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    picks["next"] = {"tool": "person_status", "args": {"name": "Ali"}, "complex": False}
    say(MA, "what about Ali")
    assert w.assistant.store.memories("pattern")
    fulls["script"] = [
        lambda m: NS(content=[NS(type="tool_use", id="t1", name="team_tasks", input={"view": "open", "person": "Ali"})]),
        lambda m: NS(content=[NS(type="text", text="Ali has 1 open task: T2610-009 Mara payroll.")]),
    ]
    say(MA, "no, I meant his tasks")
    assert last_to(chat, DM_MA) == "Ali has 1 open task: T2610-009 Mara payroll."
    assert not w.assistant.store.memories("pattern")
    assert w.assistant.store.memories("correction")
    tool_result = calls["full"][-1][-1]["content"][0]["content"]
    assert "T2610-009 Mara payroll" in tool_result


def test_complex_question_goes_to_full_ai_with_tools(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    picks["next"] = {"tool": None, "complex": True}
    fulls["script"] = [
        lambda m: NS(content=[NS(type="tool_use", id="a", name="attendance_today", input={}),
                              NS(type="tool_use", id="b", name="team_tasks", input={"view": "overdue"})]),
        lambda m: NS(content=[NS(type="text", text="Sir Amir Hussain is on leave and has the overdue Volt VAT.")]),
    ]
    say(MA, "anything I should worry about today?")
    assert last_to(chat, DM_MA).startswith("Sir Amir Hussain is on leave")
    results = calls["full"][-1][-1]["content"]
    assert "Attendance today:" in results[0]["content"] and "T2610-010 Volt VAT" in results[1]["content"]
    assert not w.assistant.store.memories("pattern")            # full AI answers are not turned into patterns


def test_general_question_answered_normally_and_builtins_skip_ai(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    picks["next"] = {"tool": None, "general": True}
    say(MA, "explain EBITDA")
    assert "AI model is not connected" in last_to(chat, DM_MA)
    n = len(calls["fast"])
    say(MA, "whoami")
    assert "user_id: 7" in last_to(chat, DM_MA) and len(calls["fast"]) == n


def test_memory_commands(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    say(MA, "remember that Aiman handles Mara and Volt")
    assert "I'll remember: Aiman handles Mara and Volt" in last_to(chat, DM_MA)
    say(MA, "what have you learned?")
    listing = last_to(chat, DM_MA)
    assert "fact: Aiman handles Mara and Volt" in listing
    mid = int(listing.splitlines()[1].split(".")[0])
    say(MA, f"forget {mid}")
    assert last_to(chat, DM_MA) == "Forgotten." and not w.assistant.store.memories("fact")
    say(MA, "learning off")
    picks["next"] = {"tool": "attendance_today", "args": {}, "complex": False}
    say(MA, "who came in today")
    assert not w.assistant.store.memories("pattern")
    assert calls["fast"] == ["who came in today"]


def test_staff_never_reach_the_brain(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    say(ALI, "what is Amir doing these days?")
    assert calls["fast"] == [] and "Attendance" not in last_to(chat, DM_ALI)


# --- v0.7.2: attendance for other days, safe tool-use replies, clearer AI errors ----------
def test_day_from_understands_common_words():
    from datetime import date
    from app.brain import day_from
    today = date(2026, 10, 8)                                      # a Thursday
    assert day_from("today", today) is None and day_from("", today) is None
    assert day_from("yesterday", today) == date(2026, 10, 7)
    assert day_from("day before yesterday", today) == date(2026, 10, 6)
    assert day_from("Monday", today) == date(2026, 10, 5)
    assert day_from("thursday", today) == date(2026, 10, 1)       # last week's, not today
    assert day_from("2026-10-02", today) == date(2026, 10, 2)
    assert day_from("5 Oct 2026", today) == date(2026, 10, 5)
    with pytest.raises(ValueError):
        day_from("2026-12-01", today)
    with pytest.raises(ValueError):
        day_from("someday", today)


def test_attendance_for_yesterday(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    picks["next"] = {"tool": "attendance_today", "args": {"date": "yesterday"}, "complex": False}
    say(MA, "who checked in late yesterday?")
    out = last_to(chat, DM_MA)
    assert out.startswith("Attendance yesterday: 1 of 3 checked in") and "in now" not in out
    assert "Did not check in: " in out
    from app.tasks import now
    assert db.last_args[0] == now().date() - timedelta(days=1)


def test_full_ai_keeps_thinking_and_drops_empty_text(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    picks["next"] = {"tool": None, "complex": True}
    fulls["script"] = [
        lambda m: NS(content=[NS(type="thinking", thinking="check tasks", signature="sig1"),
                              NS(type="text", text=""),
                              NS(type="tool_use", id="a", name="team_tasks", input={"view": "overdue"})]),
        lambda m: NS(content=[NS(type="text", text="One overdue task: Volt VAT.")]),
    ]
    say(MA, "how are we doing on deadlines?")
    assert last_to(chat, DM_MA) == "One overdue task: Volt VAT."
    sent_back = calls["full"][-1][1]["content"]
    assert sent_back[0] == {"type": "thinking", "thinking": "check tasks", "signature": "sig1"}
    assert all(b.get("text", "x") for b in sent_back) and sent_back[-1]["type"] == "tool_use"


def test_ai_error_reason_is_shown(brainy):
    chat, w, say, db, calls, picks, fulls = brainy
    picks["next"] = {"tool": None, "complex": True}

    class BadRequestError(Exception):
        body = {"error": {"type": "invalid_request_error", "message": "model: claude-x not found"}}

    def boom(m):
        raise BadRequestError("400")
    fulls["script"] = [boom]
    say(MA, "compare this week with last week")
    assert last_to(chat, DM_MA) == "My AI model is not available right now (BadRequestError: model: claude-x not found)."
