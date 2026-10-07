"""ACE's brain (v0.7): answer Sir Muhammad Ali in any wording, cheaply, and learn.

The ladder - ACE stops at the first step that can handle the message:
  1. known commands                 (assistant / employee - free)
  2. learned patterns               (memory - free): phrasings handled before
  3. fast AI   (CLAUDE_FAST_MODEL)  picks ONE read-only tool + arguments; ACE runs it and replies
  4. full AI   (CLAUDE_MODEL)       uses the tools itself, combines and reasons

Learning: when step 3 answers successfully, the phrasing is saved as a pattern with
names replaced by {person}; the next similar message is answered at step 2 without AI.
If Sir Muhammad Ali says "no / wrong / I meant ...", the pattern is forgotten and the
message goes to the full AI. He can teach facts ("remember that Aiman handles Mara"),
list ("what have you learned") and delete ("forget 12") what ACE has learned.

Only Sir Muhammad Ali (ACE_ADMINS) talks to the brain. Staff never reach these tools.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from datetime import datetime

from app.audit import audit
from app.config import settings
from app.llm import llm, usage
from app.profile import MANAGER, persona
from app.tasks import now

log = logging.getLogger(__name__)

CORRECTION = re.compile(r"^\s*(no\b|nope|wrong|that'?s (not|wrong)|not that|i meant|i mean\b|incorrect)", re.IGNORECASE)
BUILTIN = {"help", "hi", "hello", "menu", "?", "status", "skills", "files", "inbox", "ls", "whoami", "reset", "mis",
           "ask"}

# --- tools -----------------------------------------------------------------------------
TOOLS = [
    {"name": "attendance_today", "description": "Attendance for a day (default today): who checked in (time), who was "
     "late (minutes), on leave, absent or did not check in. date: 'today', 'yesterday', a weekday name or YYYY-MM-DD.",
     "input_schema": {"type": "object", "properties": {"date": {"type": "string"}}}},
    {"name": "person_status", "description": "One staff member today: check-in/out, current state, and their open "
     "tasks in the task bot (with status and due dates).",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "team_tasks", "description": "The team's tasks from the task bot. view: overdue | blocked | due_today | "
     "waiting_approval (ETA, extension or new-task approvals) | submitted | completed (last 24h) | open. Optional "
     "person and client filters.",
     "input_schema": {"type": "object", "properties": {
         "view": {"type": "string", "enum": ["overdue", "blocked", "due_today", "waiting_approval", "submitted",
                                             "completed", "open"]},
         "person": {"type": "string"}, "client": {"type": "string"}}, "required": ["view"]}},
    {"name": "followups", "description": "ACE's own open follow-ups (T-nnnn) that Sir Muhammad Ali asked ACE to chase.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "followup_detail", "description": "One of ACE's follow-ups with its history.",
     "input_schema": {"type": "object", "properties": {"ref": {"type": "string"}}, "required": ["ref"]}},
    {"name": "bot_health", "description": "Is the attendance/task bot failing to deliver chat messages? Counts and "
     "most common errors.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "client_files", "description": "Client folders and files ACE can read on the NAS; give a folder to browse it.",
     "input_schema": {"type": "object", "properties": {"folder": {"type": "string"}}}},
    {"name": "prepare_followup", "description": "Prepare a message/instruction for a staff member (ACE shows Sir "
     "Muhammad Ali a draft and waits for his OK). Pass his instruction in full.",
     "input_schema": {"type": "object", "properties": {"instruction": {"type": "string"}}, "required": ["instruction"]}},
    {"name": "remember_fact", "description": "Save a fact or preference Sir Muhammad Ali wants ACE to remember "
     "(e.g. who handles which client, how he wants things done).",
     "input_schema": {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]}},
]
READ_TOOLS = {"attendance_today", "person_status", "team_tasks", "followups", "followup_detail", "bot_health",
              "client_files"}


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def day_from(value, today=None):
    """'today' / 'yesterday' / 'day before yesterday' / a weekday (latest past one) / a date -> date or None (today)."""
    from datetime import timedelta
    from integrations.synology_chat.watcher import parse_date
    today = today or now().date()
    v = (value or "").strip().lower()
    if not v or v in ("today", "now"):
        return None
    if "before yesterday" in v:
        return today - timedelta(days=2)
    if v == "yesterday":
        return today - timedelta(days=1)
    for i, wd in enumerate(WEEKDAYS):
        if v.startswith(wd[:3]) and (v in (wd, wd[:3], "last " + wd) or v.startswith(wd)):
            back = (today.weekday() - i) % 7 or 7
            return today - timedelta(days=back)
    try:
        from datetime import date
        d = date.fromisoformat(v)
    except ValueError:
        d = parse_date(v)
    if d is None:
        raise ValueError(f"I couldn't read the date '{value}'. Try yesterday, Monday or 2026-10-05.")
    if d > today:
        raise ValueError(f"{d:%d %b %Y} is in the future.")
    return None if d == today else d


def normalize(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[^\w\s{}]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


class Brain:
    def __init__(self, assistant, fast=None, full=None) -> None:
        self.a = assistant
        self._fast = fast or self._fast_model
        self._full = full or self._full_model
        self.last: dict[str, dict] = {}

    # ------------------------------------------------------------------ models
    @staticmethod
    def _fast_model(system: str, user: str) -> dict:
        from app.assistant import _json
        r = llm.create(system, [{"role": "user", "content": user}], model=settings.claude_fast_model, max_tokens=300)
        return _json("".join(getattr(b, "text", "") for b in r.content))

    @staticmethod
    def _full_model(system: str, messages: list, tools: list):
        return llm.create(system, messages, tools=tools, max_tokens=1500)

    # ------------------------------------------------------------------ helpers
    def staff_names(self) -> list[str]:
        names = set()
        try:
            if self.a.bot:
                for p in self.a.bot.people().values():
                    if p.name:
                        names.add(p.name.lower())
                        names.add(p.name.split()[0].lower())
                    if p.chat_username:
                        names.add(p.chat_username.lower())
        except Exception:  # noqa: BLE001
            pass
        for n in self.a.chat.directory().values():
            if n:
                names.add(str(n).lower())
        return sorted((n for n in names if len(n) >= 3), key=len, reverse=True)

    def template(self, text: str) -> tuple[str, list[str]]:
        """'What is Aiman doing?' -> ('what is {person} doing', ['aiman'])."""
        t = normalize(text)
        found = []
        for name in self.staff_names():
            pat = r"\b" + re.escape(name) + r"\b"
            if re.search(pat, t):
                found.append(name)
                t = re.sub(pat, "{person}", t)
        t = re.sub(r"\b(sir)\s+\{person\}", "{person}", t)
        return t, found

    def facts(self) -> list[str]:
        return [m["value"] for m in self.a.store.memories("fact")] + \
               [m["value"] for m in self.a.store.memories("correction")][-10:]

    def context(self) -> str:
        f = self.facts()
        today = now()
        return (persona() + f"\nToday is {today:%A %d %B %Y %H:%M} ({settings.timezone}). You are answering "
                f"{MANAGER} privately (never shared with staff). Data comes from the firm's attendance + task bot "
                "(read-only) and ACE's own records."
                + ("\nThings Sir Muhammad Ali told you to remember:\n- " + "\n- ".join(f) if f else ""))

    # ------------------------------------------------------------------ tools
    def run_tool(self, name: str, args: dict, ctx: dict) -> str:
        a = self.a
        args = args or {}
        if name in ("attendance_today", "person_status", "team_tasks", "bot_health") and not a.bot:
            return "The attendance + task bot is not connected."
        if name == "attendance_today":
            try:
                on = day_from(args.get("date"))
            except ValueError as exc:
                return str(exc)
            return a.attendance_text(on)
        if name == "person_status":
            return a.person_text(args.get("name", ""))
        if name == "team_tasks":
            return a.team_tasks_text(args.get("view", "open"), args.get("person"), args.get("client"))
        if name == "followups":
            return a.list_text()
        if name == "followup_detail":
            from app.tasks import parse_ref
            t = a.store.get(parse_ref(args.get("ref", "")) or 0)
            return a.task_text(t, history=True) if t else "No such follow-up."
        if name == "bot_health":
            return a.failures_text()
        if name == "client_files":
            from app.files import list_folder
            return list_folder(args.get("folder") or "")
        if name == "prepare_followup":
            ok = a._draft(ctx["ch"], ctx["post"], args.get("instruction", ""), ctx.get("mentions", {}),
                          ctx.get("is_direct", True), ctx.get("thread"))
            ctx["drafted"] = bool(ok)
            return "Draft shown to Sir Muhammad Ali; waiting for his OK." if ok else "Could not prepare it."
        if name == "remember_fact":
            fact = (args.get("fact") or "").strip()
            if fact:
                a.store.remember("fact", normalize(fact)[:120], fact)
                audit("memory_fact", fact=fact)
            return f"Remembered: {fact}"
        return f"Unknown tool {name}"

    # ------------------------------------------------------------------ memory commands
    def memory_command(self, t: str) -> str | None:
        low = t.strip().lower()
        if re.match(r"^(what (have|did) you learn(ed|t)?|memory|your memory|show memory)\b", low):
            rows = self.a.store.memories()
            if not rows:
                return "I haven't learned anything yet."
            out = ["What I've learned (say 'forget <number>' to delete):"]
            for m in rows:
                v = m["value"]
                if m["kind"] == "pattern":
                    out.append(f"{m['id']}. phrasing '{m['key']}' -> {v.get('tool')} (used {m['uses']}x)")
                else:
                    out.append(f"{m['id']}. {m['kind']}: {v}")
            out.append(f"Learning is {'on' if self.learning else 'off'}.")
            return "\n".join(out)
        m = re.match(r"^forget\s+(\d+)\s*$", low)
        if m:
            return "Forgotten." if self.a.store.forget(int(m.group(1))) else "I have no memory with that number."
        if re.match(r"^forget\s+all\s+(patterns|phrasings)\s*$", low):
            for r in self.a.store.memories("pattern"):
                self.a.store.forget(r["id"])
            return "All learned phrasings forgotten."
        m = re.match(r"^learning\s+(on|off)\s*$", low)
        if m:
            self.a.store.set_meta("learning", m.group(1) == "on")
            return f"Learning is now {m.group(1)}."
        m = re.match(r"^(please\s+)?remember(\s+that)?\s+(.+)$", t.strip(), re.IGNORECASE | re.DOTALL)
        if m:
            fact = m.group(3).strip()
            self.a.store.remember("fact", normalize(fact)[:120], fact)
            audit("memory_fact", fact=fact)
            return f"Noted, {MANAGER}. I'll remember: {fact}"
        return None

    @property
    def learning(self) -> bool:
        return settings.learning and self.a.store.get_meta("learning", True) is not False

    # ------------------------------------------------------------------ the ladder
    def handle(self, uid: str, text: str, ctx: dict) -> str | None:
        """Answer text for Sir Muhammad Ali, or None to let ACE answer it the normal way."""
        mem = self.memory_command(text)
        if mem is not None:
            usage.route("command")
            return mem
        last = self.last.get(uid)
        if last and CORRECTION.match(text) and last.get("route") in ("learned", "fast"):
            if last.get("pattern_id"):
                self.a.store.forget(last["pattern_id"])
            self.a.store.remember("correction", normalize(text)[:120],
                                  f"When asked '{last['text']}', the answer was not what was wanted: {text}")
            return self._full_route(uid, f"My earlier question: {last['text']}\nYour answer was not right. {text}", ctx)

        # 2. learned pattern
        tpl, names = self.template(text)
        hit = self._match_pattern(tpl)
        if hit:
            mem_row, args = hit
            args = {k: (names[0] if v == "{person}" and names else v) for k, v in args.items()}
            usage.route("learned")
            self.a.store.used_memory(mem_row["id"])
            self.last[uid] = {"route": "learned", "text": text, "pattern_id": mem_row["id"]}
            audit("brain", route="learned", tool=mem_row["value"]["tool"])
            return self.run_tool(mem_row["value"]["tool"], args, ctx)

        # 3. fast AI: one read-only tool?
        if llm.configured or self._fast is not Brain._fast_model:
            try:
                pick = self._fast(self._fast_prompt(), text)
            except Exception as exc:  # noqa: BLE001
                log.warning("fast AI failed: %s", exc)
                pick = {"tool": None, "complex": True}
            tool = pick.get("tool")
            if pick.get("general") and not tool:
                usage.route("full")
                return None                               # ordinary question - normal AI answer
            if tool in READ_TOOLS and not pick.get("complex"):
                args = pick.get("args") or {}
                usage.route("fast")
                reply = self.run_tool(tool, args, ctx)
                pid = self._learn(tpl, names, tool, args) if self.learning else None
                self.last[uid] = {"route": "fast", "text": text, "pattern_id": pid}
                audit("brain", route="fast", tool=tool, learned=bool(pid))
                return reply
        # 4. full AI with tools
        return self._full_route(uid, text, ctx)

    def _fast_prompt(self) -> str:
        tools = "\n".join(f"- {t['name']}: {t['description']} args: "
                          f"{list(t['input_schema'].get('properties', {}).keys())}" for t in TOOLS if t["name"] in READ_TOOLS)
        return (f"You route messages from {MANAGER} (head of an accounting firm) to ONE data tool of his assistant ACE.\n"
                f"Tools:\n{tools}\n"
                "Reply with ONE JSON object only: {\"tool\": name or null, \"args\": {...}, \"complex\": true/false, "
                "\"general\": true/false}.\n"
                "complex = true if answering needs more than one tool, judgement, comparison or advice.\n"
                "general = true if it is a general question (accounting, tax, explanations) needing no firm data.\n"
                "Use names exactly as written in the message for person/name.")

    def _match_pattern(self, tpl: str):
        rows = self.a.store.memories("pattern")
        if not rows:
            return None
        exact = next((r for r in rows if r["key"] == tpl), None)
        if exact:
            return exact, dict(exact["value"].get("args") or {})
        best, score = None, 0.0
        for r in rows:
            if r["key"].count("{person}") != tpl.count("{person}"):
                continue
            s = difflib.SequenceMatcher(None, r["key"], tpl).ratio()
            if s > score:
                best, score = r, s
        if best and score >= 0.9:
            return best, dict(best["value"].get("args") or {})
        return None

    def _learn(self, tpl: str, names: list[str], tool: str, args: dict) -> int | None:
        if not tpl or len(tpl) < 6:
            return None
        generic = {}
        for k, v in args.items():
            if isinstance(v, str) and names and v.strip().lower() in names[:1]:
                generic[k] = "{person}"
            elif isinstance(v, str) and v.strip().lower() in names:
                return None                               # name we can't map reliably - don't learn
            else:
                generic[k] = v
        return self.a.store.remember("pattern", tpl, {"tool": tool, "args": generic})

    def _full_route(self, uid: str, text: str, ctx: dict) -> str:
        usage.route("full")
        messages = [{"role": "user", "content": text}]
        system = (self.context() + "\nUse the tools to look things up before answering questions about staff, "
                  "attendance, tasks, follow-ups or client files. To give a staff member an instruction, use "
                  "prepare_followup (never claim a message was sent). Plain text, short, key point first.")
        used = []
        try:
            for _ in range(6):
                resp = self._full(system, messages, TOOLS)
                blocks = list(resp.content)
                calls = [b for b in blocks if getattr(b, "type", "") == "tool_use"]
                if not calls:
                    answer = "".join(getattr(b, "text", "") for b in blocks).strip()
                    self.last[uid] = {"route": "full", "text": text}
                    audit("brain", route="full", tools=used)
                    if ctx.get("drafted"):
                        return ""                              # the draft itself was the reply
                    return answer or "I couldn't find an answer to that."
                messages.append({"role": "assistant", "content": [x for x in map(self._block, blocks) if x]})
                results = []
                for c in calls:
                    used.append(c.name)
                    try:
                        out = self.run_tool(c.name, dict(c.input or {}), ctx)
                    except Exception as exc:  # noqa: BLE001
                        out = f"Tool failed: {exc}"
                    results.append({"type": "tool_result", "tool_use_id": c.id, "content": out[:12000]})
                messages.append({"role": "user", "content": results})
            return "That needed too many steps - please ask in a simpler way."
        except Exception as exc:  # noqa: BLE001
            log.warning("full AI failed: %s", exc)
            return f"My AI model is not available right now ({type(exc).__name__}: {_api_reason(exc)})."

    @staticmethod
    def _block(b) -> dict | None:
        """The model's reply block as it must be sent back. Thinking blocks are kept exactly (with their
        signature); empty text blocks are dropped - the API refuses both a changed thinking block and an
        empty text block (400 BadRequest)."""
        kind = getattr(b, "type", "")
        if kind == "tool_use":
            return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
        if kind == "text":
            text = getattr(b, "text", "") or ""
            return {"type": "text", "text": text} if text.strip() else None
        if kind == "thinking":
            return {"type": "thinking", "thinking": getattr(b, "thinking", ""), "signature": getattr(b, "signature", "")}
        if kind == "redacted_thinking":
            return {"type": "redacted_thinking", "data": getattr(b, "data", "")}
        dump = getattr(b, "model_dump", None)
        return dump(exclude_none=True) if dump else None


def _api_reason(exc) -> str:
    """Short reason from an Anthropic API error (e.g. 'model: not found', 'credit balance is too low')."""
    body = getattr(exc, "body", None)
    msg = ""
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message") or ""
    msg = msg or str(exc)
    return " ".join(msg.split())[:160]


def is_builtin(text: str) -> bool:
    from app.employee import parse
    from skills.registry import by_command
    cmd, _ = parse(text)
    return cmd in BUILTIN or by_command(cmd) is not None


_ = datetime  # (kept for type hints in future tools)
