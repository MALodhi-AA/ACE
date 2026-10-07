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
    t = {"status": "open", "follow_mode": "default", "due": "2026-10-09"}
    assert T.next_reminder(t, datetime(2026, 10, 7, 11, 0, tzinfo=TZ), h) == datetime(2026, 10, 9, 10, 0, tzinfo=TZ)
    assert T.parse_ref("approve t-12") == 12
    assert timedelta(0) <= timedelta(0)
