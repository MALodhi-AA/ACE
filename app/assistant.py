"""Sir Muhammad Ali's assistant (v0.5): instructions, follow-ups, staff replies, digest.

Flow
  1. Sir Muhammad Ali writes an instruction in plain words ("ask Ali to send the Mara
     VAT working by Thursday, follow up daily").
  2. ACE turns it into a task (AI), shows what it understood and waits for "OK".
  3. ACE sends the message (privately or in a channel), follows up on schedule within
     working hours, and handles replies: done / more time / blocked / update / a
     message for Sir Muhammad Ali.
  4. Overdue tasks and escalations are reported to him; every working morning he gets
     a digest.

Only ACE_ADMINS may give instructions. Staff can reply about their own tasks and pass
messages to Sir Muhammad Ali. Attendance details are never shared with staff.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta

from app.audit import audit
from app.config import settings
from app.llm import llm
from app.profile import MANAGER, display_name
from app.tasks import Hours, TaskStore, next_reminder, now, parse_ref, ref, tz

log = logging.getLogger(__name__)

INSTRUCTION = re.compile(
    r"^\s*(?:please\s+|pls\s+|kindly\s+)?(ask|tell|remind|inform|instruct|request|follow[\s-]*up|chase|assign|"
    r"get|let|message|notify|have|send\s+(?:a\s+)?(?:message|reminder))\b", re.IGNORECASE)
YES = re.compile(r"^\s*(ok(ay)?|yes|yep|y|send( it)?|confirm(ed)?|go( ahead)?|approved?|correct|fine|done)\s*[.!]*\s*$",
                 re.IGNORECASE)
NO = re.compile(r"^\s*(no|cancel|drop|stop|don'?t( send)?|discard)\b", re.IGNORECASE)
FOR_MANAGER = re.compile(r"\b(tell|inform|message|let)\s+(?:to\s+)?(sir\s+)?(muhammad\s+ali|ma|boss|sir)\b",
                         re.IGNORECASE)


def _json(text: str) -> dict:
    """First JSON object in a model reply."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError("no JSON in reply")
    return json.loads(m.group(0))


def fmt_day(d: str | date | None) -> str:
    if not d:
        return "no due date"
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return d.strftime("%a %d-%b-%Y")


def fmt_dt(s: str | None) -> str:
    if not s:
        return "-"
    return datetime.fromisoformat(s).strftime("%a %d-%b %H:%M")


def follow_text(t: dict) -> str:
    mode = t.get("follow_mode") or "default"
    if mode == "none":
        return "no follow-ups"
    if mode == "hours":
        return f"every {t.get('follow_every'):g} working hours until done"
    if mode == "daily":
        return f"daily at {t.get('follow_time') or f'{settings.work_start:%H:%M}'} until done"
    return f"on the due date at {t.get('follow_time') or f'{settings.work_start:%H:%M}'}, then daily until done"


class Assistant:
    def __init__(self, chat, store: TaskStore | None = None, ask_model=None, bot=None, text_model=None,
                 fast_brain=None, full_brain=None) -> None:
        """`chat`: the channel watcher (post, find_channel, username, directory, channels).
        `bot`: read-only view of the attendance + task bot (None if not configured)."""
        self.chat = chat
        self.store = store or TaskStore()
        if bot is None:
            from integrations.attendance.reader import Bot
            bot = Bot() if Bot.available() else None
        self.bot = bot
        self.hours = Hours.from_settings()
        self._model = ask_model or self._ask_model
        self._text = text_model or self._llm_text
        self._fast_brain, self._full_brain = fast_brain, full_brain
        self.brain = None
        self._last_tick: datetime | None = None

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _ask_model(system: str, user: str) -> dict:
        r = llm.complete(system, [{"role": "user", "content": user}], max_tokens=1200)
        return _json(r.text)

    def is_admin(self, uid) -> bool:
        return self.chat.is_admin(uid)

    def name(self, uid) -> str:
        return display_name(self.chat.username(uid) or f"user {uid}")

    def learn_dm(self, uid, cid: int) -> None:
        dms = self.store.get_meta("dm", {})
        if dms.get(str(uid)) != cid:
            dms[str(uid)] = cid
            self.store.set_meta("dm", dms)

    def dm(self, uid) -> int | None:
        return self.store.get_meta("dm", {}).get(str(uid))

    def in_office(self, uid) -> bool | None:
        """From the attendance bot: True/False, None if unknown (not connected / not in its staff list)."""
        if not self.bot:
            return None
        try:
            return self.bot.in_office(uid, self.chat.username(uid))
        except Exception as exc:  # noqa: BLE001
            log.warning("attendance check failed: %s", exc)
            return None

    def to_manager(self, text: str) -> bool:
        """Message every admin privately (needs one message from them to ACE first)."""
        sent = False
        for uid in settings.ace_admins:
            cid = self.dm(uid)
            if cid:
                sent = bool(self.chat.post(cid, text)) or sent
        if not sent:
            log.warning("no direct chat with %s yet - message not delivered: %s", MANAGER, text[:80])
        return sent

    def directory_text(self) -> str:
        rows = []
        for uid, uname in sorted(self.chat.directory().items(), key=lambda x: str(x[1]).lower()):
            title = display_name(uname)
            rows.append(f"{uid}: {uname}" + (f" (write as {title})" if title != uname else ""))
        return "\n".join(rows) or "(no staff list available - use @mentions)"

    def resolve_user(self, who: str, mentions: dict[str, str]) -> str | None:
        """Model's 'assignee' -> Chat user id."""
        who = (who or "").strip().lstrip("@")
        if not who:
            return None
        if who.isdigit():
            return who
        directory = {**self.chat.directory(), **{int(k): v for k, v in mentions.items()}}
        low = who.lower()
        for uid, uname in directory.items():
            if low in {str(uname).lower(), display_name(uname).lower()}:
                return str(uid)
        hits = [uid for uid, uname in directory.items() if low in str(uname).lower()]
        return str(hits[0]) if len(hits) == 1 else None

    # ---------------------------------------------------------------- entry point
    def on_message(self, ch: dict, post: dict, text: str, mentions: dict[str, str], is_direct: bool,
                   reply_thread: int | None) -> bool:
        """True if the message was handled here (otherwise ACE answers it normally)."""
        if not settings.tasks_enabled:
            return False
        uid = str(post.get("creator_id", ""))
        cid = int(ch["channel_id"])
        if is_direct:
            self.learn_dm(uid, cid)
        try:
            if self.is_admin(uid):
                return self._from_manager(ch, post, text, mentions, is_direct, reply_thread)
            return self._from_staff(ch, post, text, is_direct, reply_thread)
        except Exception as exc:  # noqa: BLE001
            log.exception("assistant failed")
            audit("error", where="assistant", error=repr(exc), user_id=uid)
            self.chat.post(cid, f"Something went wrong ({type(exc).__name__}). The error has been logged.",
                           reply_thread)
            return True

    # ---------------------------------------------------------------- Sir Muhammad Ali
    def _from_manager(self, ch, post, text, mentions, is_direct, thread) -> bool:
        cid = int(ch["channel_id"])
        uid = str(post.get("creator_id"))
        say = lambda msg: self.chat.post(cid, msg, thread)  # noqa: E731
        t = text.strip()
        low = t.lower()

        # pending confirmation?
        draft = self.store.get_meta(f"draft:{uid}")
        if draft:
            if YES.match(t):
                self.store.set_meta(f"draft:{uid}", None)
                say(self._create_and_send(draft, uid))
                return True
            if NO.match(t):
                self.store.set_meta(f"draft:{uid}", None)
                say("Cancelled - nothing was sent.")
                return True

        # task commands
        if re.match(r"^\s*(my\s+|open\s+|all\s+)?tasks\b", low):
            say(self.list_text())
            return True
        if re.match(r"^\s*digest\b", low):
            say(self.digest_text())
            return True
        if self.bot and re.match(r"^\s*(who(\s+is|'s)\s+in|attendance|team(\s+today)?|who\s+is\s+(absent|late))\b", low):
            say(self.attendance_text())
            return True
        if self.bot and re.match(r"^\s*(bot\s+errors?|delivery\s+(errors?|failures?)|bot\s+health)\b", low):
            say(self.failures_text())
            return True
        m2 = re.match(r"^\s*(?:what\s+(?:is|are)\s+|what's\s+)?(\w+)(?:\s+\w+)?\s+(?:currently\s+|now\s+)?working\s+on"
                      r"(?:\s+(?:now|today|currently))?\??\s*$", t, re.IGNORECASE) or \
            re.match(r"^\s*(?:bot\s+)?tasks\s+(?:of|for)\s+(.+?)\s*$", t, re.IGNORECASE)
        if self.bot and m2:
            say(self.person_text(m2.group(1)))
            return True
        if self.bot and re.match(r"^\s*(bot\s+tasks|overdue(\s+tasks)?|team\s+tasks)\b", low):
            say(self.bot_tasks_text())
            return True
        m = re.match(r"^\s*(task|show|cancel|close|done|complete|approve|reject|remind|pause|resume)\s+(?:task\s+)?T-?\d+",
                     t, re.IGNORECASE)
        if m:
            say(self._command(m.group(1).lower(), t, uid))
            return True

        # a correction to the pending draft
        if draft and not INSTRUCTION.match(t):
            return self._draft(ch, post, t, mentions, is_direct, thread, previous=draft)
        if INSTRUCTION.match(t):
            return self._draft(ch, post, t, mentions, is_direct, thread)
        return self._think(ch, post, t, mentions, is_direct, thread)

    def _think(self, ch, post, text, mentions, is_direct, thread) -> bool:
        """Steps 2-4 of the ladder (learned pattern, fast AI, full AI) - in the background."""
        from app.brain import Brain, is_builtin
        if is_builtin(text) and not text.strip().lower().startswith("ask "):
            from app.llm import usage
            usage.route("command")
            return False                                  # status, files, mis ... answered as before
        if getattr(self, "brain", None) is None:
            self.brain = Brain(self, fast=self._fast_brain, full=self._full_brain)
        cid = int(ch["channel_id"])
        uid = str(post.get("creator_id"))
        ctx = {"ch": ch, "post": post, "mentions": mentions, "is_direct": is_direct, "thread": thread}

        def work():
            try:
                reply = self.brain.handle(uid, text, ctx)
            except Exception as exc:  # noqa: BLE001
                log.exception("brain failed")
                reply = f"Something went wrong ({type(exc).__name__}). The error has been logged."
            if reply is None:
                self.chat.answer_normally(ch, post, text)
            elif reply:
                self.chat.post(cid, reply, thread)

        runner = getattr(self.chat, "run_later", None)
        (runner or (lambda f: f()))(work)
        return True

    @staticmethod
    def _llm_text(system: str, user: str) -> str:
        return llm.complete(system, [{"role": "user", "content": user}], max_tokens=1200).text

    def _draft(self, ch, post, text, mentions, is_direct, thread, previous: dict | None = None) -> bool:
        cid = int(ch["channel_id"])
        uid = str(post.get("creator_id"))
        today = now()
        channels = ", ".join(sorted(self.chat.channel_names()))
        here = "" if is_direct else self.chat.folder_name(ch)
        system = f"""You turn instructions from {MANAGER} (head of an accounting and tax firm in the UAE)
into a task for a staff member. Reply with ONE JSON object only, no other text:
{{"is_instruction": true/false,
 "assignee": "user id or exact user name from the staff list, or null",
 "title": "short task title, e.g. 'Mara Q3 VAT working'",
 "client": "client name or null",
 "message": "the message ACE will send to the staff member: polite, plain text, starts with their name (with title if listed), says '{MANAGER} has asked ...', states the due date as e.g. Thu 09-Oct-2026 if there is one",
 "due": "YYYY-MM-DD or null",
 "delivery": "private" or "channel",
 "channel": "channel name for channel delivery, else null",
 "when": "now" or "checkin",
 "follow_up": {{"mode": "default|daily|hours|none", "time": "HH:MM or null", "every_hours": number or null}},
 "escalate_at": "YYYY-MM-DD HH:MM or null",
 "unclear": "what is missing or ambiguous, or null"}}
Rules: today is {today:%A %d %B %Y %H:%M} ({settings.timezone}). Working hours {settings.work_start:%H:%M}-{settings.work_end:%H:%M} on working days.
"by Thursday" means the coming Thursday. "follow up daily" -> mode daily (time null unless a time is given - ACE then uses the start of working hours, {settings.work_start:%H:%M});
"every 2 hours" -> mode hours; no follow-up words -> mode default; "don't follow up" -> none.
"ask when he checks in" -> when checkin, otherwise now. "tell me if not done by X" -> escalate_at.
Delivery: "privately"/"in private" -> private; "in <channel>"/"in the group" -> channel.
If not said: {'channel ' + repr(here) if here else 'private'}.
If it is not an instruction for a staff member (e.g. a question), set is_instruction false.
Staff list (id: user name):
{self.directory_text()}
Mentioned in the message: {json.dumps(mentions) or '{}'}
Channels ACE is in: {channels}
Things {MANAGER} told ACE to remember: {"; ".join(str(m["value"]) for m in self.store.memories("fact")) or "-"}"""
        user = text if not previous else (
            f"Previous draft: {json.dumps(previous['parsed'])}\nCorrection from {MANAGER}: {text}\n"
            "Return the full corrected JSON.")
        try:
            parsed = self._model(system, user)
        except Exception as exc:  # noqa: BLE001
            log.warning("instruction parse failed: %s", exc)
            if previous:
                self.chat.post(cid, "I couldn't understand that change - please say it another way, "
                                    "or reply OK to send the draft as it is.", thread)
                return True
            return False
        if not parsed.get("is_instruction") and not previous:
            return False
        draft = {"parsed": parsed, "text": text if not previous else previous["text"] + " / " + text,
                 "from_cid": cid, "from_thread": thread, "mentions": mentions}
        problems = []
        assignee = self.resolve_user(parsed.get("assignee") or "", mentions)
        if not assignee:
            problems.append(f"I don't know who '{parsed.get('assignee') or '?'}' is. Use their Chat name "
                            "or @mention them.")
        draft["assignee_id"] = assignee
        target = None
        if parsed.get("delivery") == "channel":
            ch2 = self.chat.find_channel(parsed.get("channel") or "") if parsed.get("channel") else (
                None if is_direct else ch)
            if not ch2:
                problems.append(f"I'm not in a channel called '{parsed.get('channel')}'.")
            else:
                target = int(ch2["channel_id"])
                draft["channel_name"] = self.chat.folder_name(ch2)
        elif assignee and not self.dm(assignee):
            problems.append(f"I can't message {self.name(assignee)} privately yet - they need to send me "
                            "any message once (e.g. 'hi'). Or tell me to post it in a channel.")
        draft["target_cid"] = target
        if parsed.get("unclear"):
            problems.append(f"Note: {parsed['unclear']}")
        self.store.set_meta(f"draft:{uid}", draft)
        self.chat.post(cid, self._confirm_text(draft, problems), thread)
        return True

    def _confirm_text(self, d: dict, problems: list[str]) -> str:
        p = d["parsed"]
        fu = p.get("follow_up") or {}
        fake = {"follow_mode": fu.get("mode") or "default", "follow_time": fu.get("time"),
                "follow_every": fu.get("every_hours")}
        to = self.name(d["assignee_id"]) if d.get("assignee_id") else (p.get("assignee") or "?")
        where = f"in {d['channel_name']}" if d.get("target_cid") else "privately"
        lines = [f"Please confirm, {MANAGER}:",
                 f"Task: {p.get('title')}" + (f" ({p['client']})" if p.get("client") else ""),
                 f"To: {to}, {where}" + (" - when they check in" if p.get("when") == "checkin" else ""),
                 f"Due: {fmt_day(p.get('due'))}",
                 f"Follow-up: {follow_text(fake)}",
                 f"Tell you if still open: {p.get('escalate_at') or 'when it becomes overdue'}",
                 "", "Message:", p.get("message") or "", ""]
        if problems:
            lines += problems + [""]
        lines.append("Reply OK to send, tell me what to change, or cancel.")
        return "\n".join(lines)

    def _create_and_send(self, d: dict, by: str) -> str:
        p = d["parsed"]
        if not d.get("assignee_id"):
            return "I still don't know who this is for - please give me the instruction again with their name."
        fu = p.get("follow_up") or {}
        due = p.get("due")
        esc = p.get("escalate_at")
        task = self.store.add(
            title=p.get("title") or "Task", client=p.get("client"), message=p.get("message") or "",
            assignee_id=str(d["assignee_id"]), assignee_name=self.name(d["assignee_id"]),
            delivery="channel" if d.get("target_cid") else "private", channel_id=d.get("target_cid"),
            due=due, follow_mode=fu.get("mode") or "default", follow_time=fu.get("time"),
            follow_every=fu.get("every_hours"),
            escalate_at=datetime.fromisoformat(esc).replace(tzinfo=tz()).isoformat() if esc else None,
            created_by=by)
        if p.get("when") == "checkin":
            here = self.in_office(task["assignee_id"])
            if here is False:
                self.store.update(task["id"], wait_checkin=1, reminders=-1, next_remind_at=now().isoformat())
                audit("task_created", task=ref(task["id"]), deliver="on check-in")
                return (f"{ref(task['id'])} saved. {task['assignee_name']} hasn't checked in yet - I'll send it "
                        "as soon as they do.")
            if here is None:
                send_at = self.hours.next_open(now())
                if send_at > now() + timedelta(minutes=1):
                    self.store.update(task["id"], next_remind_at=send_at.isoformat(), reminders=-1)
                    audit("task_created", task=ref(task["id"]), deliver_at=send_at.isoformat())
                    return (f"{ref(task['id'])} saved. I can't see {task['assignee_name']} in the attendance "
                            f"system, so I'll send it when working hours start ({fmt_dt(send_at.isoformat())}).")
        ok = self._deliver(task)
        audit("task_created", task=ref(task["id"]), assignee=task["assignee_id"], sent=ok)
        if not ok:
            return (f"{ref(task['id'])} saved, but I couldn't post the message. I'll keep it open - "
                    f"tell me 'remind {ref(task['id'])}' to try again.")
        return f"Sent. {ref(task['id'])} - I'll follow up {follow_text(self.store.get(task['id']))}."

    def _deliver(self, task: dict) -> bool:
        cid = task["channel_id"] if task["delivery"] == "channel" else self.dm(task["assignee_id"])
        if not cid:
            return False
        text = (f"{task['message']}\n\n({ref(task['id'])} - reply here with 'done' when finished, or tell me "
                "if you need more time or anything is blocking you.)")
        post_id = self.chat.post(int(cid), text)
        if not post_id:
            return False
        sent = now()
        upd = {"post_id": post_id if post_id > 1 else None, "channel_id": int(cid),
               "sent_at": sent.isoformat(timespec="seconds"), "reminders": 0}
        nxt = next_reminder({**task, "status": "open"}, sent, self.hours)
        upd["next_remind_at"] = nxt.isoformat() if nxt else None
        self.store.update(task["id"], **upd)
        self.store.event(task["id"], "sent", "ACE", text)
        return True

    def _command(self, verb: str, text: str, by: str) -> str:
        tid = parse_ref(text)
        t = self.store.get(tid) if tid else None
        if not t:
            return f"I can't find {ref(tid) if tid else 'that task'}."
        r = ref(tid)
        note = re.sub(r"^\s*\w+\s+(?:task\s+)?T-?\d+\s*", "", text, flags=re.IGNORECASE).strip()
        if verb in ("task", "show"):
            return self.task_text(t, history=True)
        if t["status"] != "open" and verb not in ("task", "show"):
            return f"{r} is already {t['status']}."
        if verb == "cancel":
            self.store.update(tid, status="cancelled", closed_at=now().isoformat(timespec="seconds"), close_note=note)
            self.store.event(tid, "cancelled", by, note)
            return f"{r} cancelled. I won't follow up on it any more."
        if verb in ("close", "done", "complete"):
            self.store.update(tid, status="done", closed_at=now().isoformat(timespec="seconds"),
                              close_note=note or f"closed by {MANAGER}")
            self.store.event(tid, "done", by, note)
            return f"{r} marked done."
        if verb == "approve":
            if not t["ext_due"]:
                return f"{r} has no extension request."
            self.store.update(tid, due=t["ext_due"], ext_due=None, overdue_notified=0, escalated=0,
                              next_remind_at=(next_reminder({**t, "due": t["ext_due"]}, now(), self.hours)
                                              or now()).isoformat())
            self.store.event(tid, "extension_approved", by, t["ext_due"])
            self._notify_assignee(t, f"{MANAGER} approved more time for {r} ({t['title']}). "
                                     f"New due date: {fmt_day(t['ext_due'])}.")
            return f"Approved - {r} is now due {fmt_day(t['ext_due'])}. I've told {t['assignee_name']}."
        if verb == "reject":
            if not t["ext_due"]:
                return f"{r} has no extension request."
            self.store.update(tid, ext_due=None, ext_reason=None)
            self.store.event(tid, "extension_rejected", by, note)
            self._notify_assignee(t, f"{MANAGER} could not approve more time for {r} ({t['title']}). "
                                     f"It stays due {fmt_day(t['due'])}." + (f" Note: {note}" if note else ""))
            return f"Rejected - I've told {t['assignee_name']} {r} stays due {fmt_day(t['due'])}."
        if verb == "remind":
            if not t["sent_at"]:
                return f"Sent {r} now." if self._deliver(t) else f"I still couldn't post {r}."
            self._remind(t)
            return f"Reminder sent for {r}."
        if verb == "pause":
            self.store.update(tid, paused=1)
            self.store.event(tid, "paused", by, "")
            return f"Follow-ups for {r} paused. Say 'resume {r}' to continue."
        if verb == "resume":
            self.store.update(tid, paused=0, next_remind_at=(next_reminder(t, now(), self.hours) or now()).isoformat())
            self.store.event(tid, "resumed", by, "")
            return f"Follow-ups for {r} resumed."
        return "?"

    # ---------------------------------------------------------------- staff
    def _from_staff(self, ch, post, text, is_direct, thread) -> bool:
        uid = str(post.get("creator_id"))
        cid = int(ch["channel_id"])
        tasks = self.store.open_tasks(uid)
        on_task_thread = bool(thread and self.store.by_post(thread))
        wants_manager = bool(FOR_MANAGER.search(text))
        if not tasks and not wants_manager:
            return False
        listing = "\n".join(f"{ref(t['id'])}: {t['title']} (due {t['due'] or '-'})" for t in tasks) or "(none)"
        system = f"""A staff member of an accounting firm is writing to ACE, the AI assistant of {MANAGER}.
Classify the message. Reply with ONE JSON object only:
{{"task": "T-xxxx or null", "kind": "done|extension|blocked|update|for_manager|other",
 "new_due": "YYYY-MM-DD or null", "summary": "one line, in plain English"}}
kind: done = the task is finished; extension = asks for more time; blocked = cannot proceed / waiting on
someone; update = progress information; for_manager = a message to pass to {MANAGER};
other = a general question or chat for ACE itself.
Today is {now():%A %d %B %Y}. Their open tasks:
{listing}
{"The message is a reply in the thread of task " + ref(self.store.by_post(thread)['id']) + "." if on_task_thread else ""}"""
        try:
            c = self._model(system, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("staff message classify failed: %s", exc)
            return False
        kind = c.get("kind") or "other"
        if kind == "other":
            return False
        name = self.name(uid)
        summary = c.get("summary") or text
        say = lambda msg: self.chat.post(cid, msg, thread)  # noqa: E731
        if kind == "for_manager":
            ok = self.to_manager(f"Message from {name}: {text}")
            say(f"I've passed your message to {MANAGER}." if ok else
                f"I couldn't reach {MANAGER} right now - I'll keep trying.")
            audit("staff_message_for_manager", user_id=uid, delivered=ok)
            return True
        tid = parse_ref(c.get("task") or "") or (self.store.by_post(thread)["id"] if on_task_thread else None)
        if tid is None and len(tasks) == 1:
            tid = tasks[0]["id"]
        t = self.store.get(tid) if tid else None
        if not t or t["assignee_id"] != uid or t["status"] != "open":
            say("Which task is this about? Please mention its number, e.g. " +
                ", ".join(ref(x["id"]) for x in tasks[:5]) + ".")
            return True
        r = ref(t["id"])
        now_s = now().isoformat(timespec="seconds")
        if kind == "done":
            self.store.update(t["id"], status="done", closed_at=now_s, close_note=summary)
            self.store.event(t["id"], "done", uid, text)
            say(f"Thank you, {name}. I've marked {r} done and informed {MANAGER}.")
            self.to_manager(f"Done: {r} {t['title']} - {name}: {summary}")
        elif kind == "extension":
            new_due = c.get("new_due")
            self.store.update(t["id"], ext_due=new_due, ext_reason=summary, last_update=now_s)
            self.store.event(t["id"], "extension_requested", uid, text)
            say(f"I've asked {MANAGER} about more time for {r}. I'll let you know.")
            self.to_manager(f"{name} asks for more time on {r} {t['title']} "
                            f"(due {fmt_day(t['due'])} -> {fmt_day(new_due) if new_due else 'no date given'}): "
                            f"{summary}\nReply 'approve {r}' or 'reject {r}'.")
        elif kind == "blocked":
            self.store.update(t["id"], last_update=now_s)
            self.store.event(t["id"], "blocked", uid, text)
            say(f"Noted - I've told {MANAGER} that {r} is blocked.")
            self.to_manager(f"Blocked: {r} {t['title']} - {name}: {summary}")
        else:  # update
            self.store.update(t["id"], last_update=now_s)
            self.store.event(t["id"], "update", uid, text)
            say(f"Thanks, noted on {r}.")
        audit("task_reply", task=r, kind=kind, user_id=uid)
        return True

    def _notify_assignee(self, t: dict, text: str) -> None:
        cid = t["channel_id"] or self.dm(t["assignee_id"])
        if cid:
            self.chat.post(int(cid), text, t["post_id"] if t["delivery"] == "channel" else None)

    # ---------------------------------------------------------------- schedule
    def tick(self, force: bool = False) -> None:
        """Follow-ups, escalations, overdue notices and the morning digest (about once a minute)."""
        t0 = now()
        if not force and self._last_tick and (t0 - self._last_tick).total_seconds() < 55:
            return
        self._last_tick = t0
        for t in self.store.open_tasks():
            try:
                self._tick_task(t, t0)
            except Exception as exc:  # noqa: BLE001
                log.warning("task %s: %s", ref(t["id"]), exc)
        start = datetime.combine(t0.date(), settings.digest_time, tz())
        if t0.weekday() in self.hours.days and start <= t0 < start + timedelta(hours=3) \
                and self.store.get_meta("digest_day") != t0.date().isoformat():
            self.store.set_meta("digest_day", t0.date().isoformat())
            self.to_manager(self.digest_text())

    def _tick_task(self, t: dict, t0: datetime) -> None:
        if t["paused"]:
            return
        if t["next_remind_at"] and datetime.fromisoformat(t["next_remind_at"]) <= t0:
            here = self.in_office(t["assignee_id"])
            # with attendance: only while the person is checked in; without: within working hours
            ok_now = here if here is not None else (self.hours.is_open(t0) and not t.get("wait_checkin"))
            if not t["sent_at"]:
                if ok_now or (here is None and t.get("wait_checkin") and self.hours.is_open(t0)):
                    self._deliver(t)                   # held until check-in / working hours
            elif ok_now:
                self._remind(t)
        if t["escalate_at"] and not t["escalated"] and datetime.fromisoformat(t["escalate_at"]) <= t0:
            self.store.update(t["id"], escalated=1)
            self.to_manager(f"Still open: {ref(t['id'])} {t['title']} ({t['assignee_name']}, due "
                            f"{fmt_day(t['due'])}). {self._last_word(t)}")
        if t["due"] and not t["overdue_notified"]:
            due_end = datetime.combine(date.fromisoformat(t["due"]), settings.work_end, tz())
            if t0 >= due_end:
                self.store.update(t["id"], overdue_notified=1)
                self.store.event(t["id"], "overdue", "ACE", "")
                self.to_manager(f"Overdue: {ref(t['id'])} {t['title']} ({t['assignee_name']}, was due "
                                f"{fmt_day(t['due'])}). {self._last_word(t)}")

    def _remind(self, t: dict) -> None:
        n = (t["reminders"] or 0) + 1
        due = f", due {fmt_day(t['due'])}" if t["due"] else ""
        late = " - now overdue" if t["due"] and date.fromisoformat(t["due"]) < now().date() else ""
        text = (f"Reminder{f' ({n})' if n > 1 else ''} from ACE: {ref(t['id'])} {t['title']}{due}{late}. "
                f"{t['assignee_name']}, please reply with 'done' or a short status.")
        thread = t["post_id"] if t["delivery"] == "channel" else None
        self.chat.post(int(t["channel_id"]), text, thread)
        nxt = next_reminder(t, now(), self.hours)
        self.store.update(t["id"], reminders=n, next_remind_at=nxt.isoformat() if nxt else None)
        self.store.event(t["id"], "reminder", "ACE", text)

    def _last_word(self, t: dict) -> str:
        ev = [e for e in self.store.events(t["id"]) if e["kind"] in ("update", "blocked", "extension_requested")]
        if not ev:
            return f"No reply yet after {t['reminders'] or 0} reminder(s)."
        return f"Last from them ({fmt_dt(ev[-1]['ts'])}): {ev[-1]['text'][:200]}"

    # ---------------------------------------------------------------- reports
    def task_text(self, t: dict, history: bool = False) -> str:
        lines = [f"{ref(t['id'])} {t['title']}" + (f" ({t['client']})" if t["client"] else ""),
                 f"To: {t['assignee_name']} ({t['delivery']}) - status {t['status']}",
                 f"Due: {fmt_day(t['due'])} - follow-up {follow_text(t)}, {t['reminders'] or 0} reminder(s) sent"]
        if t["ext_due"]:
            lines.append(f"Waiting for your approval: more time to {fmt_day(t['ext_due'])} ({t['ext_reason']})")
        if history:
            lines.append("History:")
            for e in self.store.events(t["id"])[-10:]:
                lines.append(f"- {fmt_dt(e['ts'])} {e['kind']}: {(e['text'] or '')[:120]}")
        return "\n".join(lines)

    def list_text(self) -> str:
        tasks = self.store.open_tasks()
        if not tasks:
            return "No open tasks."
        today = now().date()
        out = [f"Open tasks ({len(tasks)}):"]
        for t in sorted(tasks, key=lambda x: x["due"] or "9999"):
            flag = " OVERDUE" if t["due"] and date.fromisoformat(t["due"]) < today else ""
            ext = " (asks more time)" if t["ext_due"] else ""
            out.append(f"- {ref(t['id'])} {t['title']} - {t['assignee_name']} - due {fmt_day(t['due'])}{flag}{ext}")
        out.append("Commands: task T-n, remind T-n, close T-n, cancel T-n, pause T-n, approve/reject T-n")
        return "\n".join(out)

    def digest_text(self) -> str:
        t0 = now()
        today = t0.date()
        tasks = self.store.open_tasks()
        due_today = [t for t in tasks if t["due"] == today.isoformat()]
        overdue = [t for t in tasks if t["due"] and date.fromisoformat(t["due"]) < today]
        asks = [t for t in tasks if t["ext_due"]]
        since = datetime.combine(today - timedelta(days=1), time(0, 0), tz())
        done = self.store.events_since(since, ("done",))
        blocked = self.store.events_since(since, ("blocked",))
        lines = [f"Good morning {MANAGER} - {t0:%a %d %b %Y}", ""]
        if self.bot:
            try:
                lines += self.attendance_lines() + [""] + self.bot_task_lines() + [""]
                f = self.bot.delivery_failures()
                if f["hour"]:
                    lines += [f"Warning: the task bot failed to deliver {f['hour']:,} chat messages in the last hour "
                              f"({f['day']:,} in 24h). Send 'bot errors' for details.", ""]
            except Exception as exc:  # noqa: BLE001
                lines += [f"(Attendance / task bot not readable: {exc})", ""]
            lines.append("My follow-ups:")

        def section(title, rows, fmt):
            lines.append(f"{title}: {len(rows)}")
            lines.extend(f"- {fmt(r)}" for r in rows[:15])

        section("Due today", due_today, lambda t: f"{ref(t['id'])} {t['title']} ({t['assignee_name']})")
        section("Overdue", overdue, lambda t: f"{ref(t['id'])} {t['title']} ({t['assignee_name']}, due "
                                              f"{fmt_day(t['due'])}, {t['reminders'] or 0} reminders)")
        section("Waiting for your approval", asks,
                lambda t: f"{ref(t['id'])} {t['assignee_name']} asks to move to {fmt_day(t['ext_due'])}")
        section("Blocked since yesterday", blocked, lambda e: f"{ref(e['task_id'])}: {(e['text'] or '')[:100]}")
        section("Completed since yesterday", done, lambda e: f"{ref(e['task_id'])} {self._title(e['task_id'])}")
        lines.append(f"Open tasks in total: {len(tasks)}")
        files = self.chat.files_saved_since(since)
        if files is not None:
            lines.append(f"Files saved from chats since yesterday: {files}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- from the attendance + task bot
    def _bot_name(self, emp_id) -> str:
        p = self.bot.people().get(int(emp_id or 0)) if self.bot else None
        return display_name(p.name) if p else f"employee {emp_id}"

    def attendance_lines(self, on: date | None = None) -> list[str]:
        from integrations.attendance.reader import fmt_local
        today = now().date()
        on = on or today
        past = on < today
        days = self.bot.day(on) if past else self.bot.today()
        when = "today" if not past else ("yesterday" if on == today - timedelta(days=1) else f"on {on:%a %d %b %Y}")
        present = [d for d in days if d.check_in]
        in_now = [d for d in days if d.in_office]
        late = sorted((d for d in present if d.late_minutes > 0), key=lambda d: -d.late_minutes)
        leave = [d for d in days if d.day_type == "leave"]
        absent = [d for d in days if d.day_type == "absent"]
        missing = [d for d in days if d.day_type in ("none", "present") and not d.check_in]
        nm = lambda d: display_name(d.person.name)  # noqa: E731
        out = [f"Attendance {when}: {len(present)} of {len(days)} checked in"
               + ("" if past else f", {len(in_now)} in now")]
        if past and not present and not leave and not absent:
            out.append("No attendance records for that day (holiday or weekend?).")
            return out
        if late:
            out.append("Late: " + ", ".join(f"{nm(d)} ({fmt_local(d.check_in, d.person.tz)}, +{d.late_minutes} min)"
                                            for d in late))
        if leave:
            out.append("On leave: " + ", ".join(nm(d) + (f" ({d.leave_reason})" if d.leave_reason else "") for d in leave))
        if absent:
            out.append("Absent: " + ", ".join(nm(d) for d in absent))
        if missing:
            out.append(("Did not check in: " if past else "Not checked in yet: ") + ", ".join(nm(d) for d in missing))
        return out

    def attendance_text(self, on: date | None = None) -> str:
        try:
            return "\n".join(self.attendance_lines(on))
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't read the attendance system: {exc}"

    def bot_task_lines(self, limit: int = 10) -> list[str]:
        from integrations.attendance.reader import fmt_due
        t = self.bot.tasks()
        row = lambda r: f"{r['task_code']} {r['title'][:70]} - {self._bot_name(r['emp'])} (due {fmt_due(r['due'])})"  # noqa: E731
        out = [f"Team tasks (task bot): {len(t['open'])} open, {len(t['overdue'])} overdue, "
               f"{len(t['due_today'])} due today, {len(t['completed'])} completed in the last 24h"]
        sections = [("Overdue", t["overdue"]), ("Blocked", t["blocked"]),
                    ("ETA waiting for approval", t["eta_waiting"]),
                    ("Extension requests waiting", t["ext_waiting"]),
                    ("Submitted, waiting for review", t["submitted"]),
                    ("New tasks waiting for approval", t["create_waiting"])]
        for title, rows in sections:
            if rows:
                out.append(f"{title}: {len(rows)}")
                out.extend(f"- {row(r)}" for r in sorted(rows, key=lambda r: r["due"] or datetime.max.replace(
                    tzinfo=tz()))[:limit])
                if len(rows) > limit:
                    out.append(f"- ... and {len(rows) - limit} more")
        return out

    def team_tasks_text(self, view: str = "open", person: str | None = None, client: str | None = None) -> str:
        from integrations.attendance.reader import fmt_due
        try:
            t = self.bot.tasks()
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't read the task bot: {exc}"
        key = {"waiting_approval": None}.get(view, view)
        rows = (t["eta_waiting"] + t["ext_waiting"] + t["create_waiting"]) if view == "waiting_approval" else t.get(key, t["open"])
        if person:
            low = person.strip().lower()
            ids = {p.employee_id for p in self.bot.people().values()
                   if low in (p.name or "").lower() or low == p.chat_username.lower()}
            rows = [r for r in rows if int(r["emp"] or 0) in ids]
        if client:
            c = client.strip().lower()
            rows = [r for r in rows if c in (r["title"] or "").lower() or c in (r.get("channel_name") or "").lower()]
        title = view.replace("_", " ") + (f" - {person}" if person else "") + (f" - {client}" if client else "")
        out = [f"Team tasks ({title}): {len(rows)}"]
        for r in sorted(rows, key=lambda r: r["due"] or datetime.max.replace(tzinfo=tz()))[:30]:
            out.append(f"- {r['task_code']} {r['title'][:80]} - {self._bot_name(r['emp'])} - {r['status']} "
                       f"(due {fmt_due(r['due'])})")
        if len(rows) > 30:
            out.append(f"- ... and {len(rows) - 30} more")
        return "\n".join(out)

    def bot_tasks_text(self) -> str:
        try:
            return "\n".join(self.bot_task_lines(limit=25))
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't read the task bot: {exc}"

    def person_text(self, who: str) -> str:
        from integrations.attendance.reader import fmt_due, fmt_local
        uid = self.resolve_user(who, {})
        p = self.bot.person_for_chat(uid or "", self.chat.username(uid) if uid else who)
        if not p:
            low = who.strip().lower()
            p = next((x for x in self.bot.people().values() if low and low in x.name.lower()), None)
        if not p:
            return f"I can't find '{who}' in the attendance system."
        d = self.bot.day_for(p)
        if d and d.check_in:
            state = (f"checked in {fmt_local(d.check_in, p.tz)}" + (f" (+{d.late_minutes} min late)" if d.late_minutes else "")
                     + (f", checked out {fmt_local(d.check_out, p.tz)}" if d.check_out else f", now {d.status or 'working'}"))
        else:
            state = {"leave": "on leave", "absent": "absent"}.get(d.day_type if d else "", "not checked in yet")
        rows = self.bot.tasks_of(p)
        out = [f"{display_name(p.name)} today: {state}", f"Open tasks in the task bot: {len(rows)}"]
        for r in sorted(rows, key=lambda r: r["due"] or datetime.max.replace(tzinfo=tz()))[:15]:
            out.append(f"- {r['task_code']} {r['title'][:80]} - {r['status']} (due {fmt_due(r['due'])})")
        mine = [t for t in self.store.open_tasks() if self.bot.person_for_chat(t["assignee_id"], t["assignee_name"]) == p]
        if mine:
            out.append(f"My follow-ups with them: " + ", ".join(f"{ref(t['id'])} {t['title']}" for t in mine))
        return "\n".join(out)

    def failures_text(self) -> str:
        try:
            f = self.bot.delivery_failures()
        except Exception as exc:  # noqa: BLE001
            return f"I couldn't read the task bot's delivery log: {exc}"
        if not f["hour"] and not f["day"]:
            return "Task bot: no failed chat deliveries in the last 24 hours."
        out = [f"Task bot - failed chat deliveries: {f['hour']:,} in the last hour, {f['day']:,} in 24 hours"]
        if f["top"]:
            out.append("Most common errors (last hour):")
            out.extend(f"- {r['n']:,} x {r['source'] or '?'}: {(r['error'] or '').strip()}" for r in f["top"])
        if f["channels"]:
            out.append("Channels most affected: " + ", ".join(f"{r['channel'] or '?'} ({r['n']:,})" for r in f["channels"]))
        return "\n".join(out)

    def _title(self, tid: int) -> str:
        t = self.store.get(tid)
        return t["title"] if t else ""

    def status_line(self) -> str:
        tasks = self.store.open_tasks()
        today = now().date()
        late = sum(1 for t in tasks if t["due"] and date.fromisoformat(t["due"]) < today)
        dm_ok = any(self.dm(a) for a in settings.ace_admins)
        staff = len(self.chat.directory())
        att = ""
        if self.bot:
            try:
                people = self.bot.people()
                known = {str(u) for u in self.chat.directory()} | {str(n).lower() for n in self.chat.directory().values()}
                matched = sum(1 for p in people.values() if p.chat_ref in known or p.chat_username.lower() in known)
                att = f"\nAttendance + task bot ✓ - {len(people)} staff, {matched} matched to Chat users"
            except Exception as exc:  # noqa: BLE001
                att = f"\nAttendance + task bot ✗ - {exc}"
        from app.llm import usage
        att += "\n" + usage.line()
        return att.lstrip("\n") + ("\n" if att else "") + (f"Tasks: {len(tasks)} open, {late} overdue; digest {settings.digest_time:%H:%M} "
                f"{'✓' if dm_ok else '(send me any direct message once so I can reach you)'}; "
                f"staff list: {staff} people" + ("" if staff else " - use @mentions in instructions"))
