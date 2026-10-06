"""ACE - the employee.

Ties together identity (config/employee.yaml), skills, permissions, the AI
model and the audit log. Chat integrations call `handle()` and never talk to
skills or the model directly.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import yaml

from app.audit import audit
from app.config import ROOT_DIR, settings
from app.files import FileAccessError, get_sources, list_folder
from app.nas import nas
from app.llm import llm
from skills.registry import SKILLS, by_command

log = logging.getLogger(__name__)

PROFILE = yaml.safe_load((ROOT_DIR / "config" / "employee.yaml").read_text(encoding="utf-8"))
STARTED_AT = datetime.now(ZoneInfo(settings.timezone))

# short per-user conversation memory for `ask` (in memory; resets on restart)
_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=8))
_history_lock = threading.Lock()


@dataclass
class Response:
    """`text` is returned immediately. If `background` is set, it is run after
    the HTTP response and its return value is sent as a follow-up message."""
    text: str
    background: Callable[[], str] | None = None


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def is_allowed(user_id: str, username: str) -> bool:
    allowed = settings.allowed_users
    if not allowed:
        return True
    keys = {user_id.lower(), username.lower()}
    return any(a.lower() in keys for a in allowed)


# ---------------------------------------------------------------------------
# Command parsing
# ---------------------------------------------------------------------------

# Accepts "/ace ...", "/ai ...", "@ACE ..." or "ACE, ..." in front of a command
_PREFIX = re.compile(r"^\s*(?:/ace\b|/ai\b|@ace\b|ace(?=[:,]))[:,]?\s*", re.IGNORECASE)


def parse(text: str) -> tuple[str, str]:
    text = _PREFIX.sub("", text or "").strip()
    if not text:
        return "help", ""
    parts = text.split(None, 1)
    return parts[0].lower().lstrip("/"), (parts[1].strip() if len(parts) > 1 else "")


# ---------------------------------------------------------------------------
# Built-in commands
# ---------------------------------------------------------------------------

def _tick(ok: bool) -> str:
    return "✓" if ok else "✗"


def status_text(check_ai: bool = True) -> str:
    ai_ok, ai_note = llm.ping() if check_ai else (settings.ai_configured, settings.claude_model)
    lines = [
        f"{PROFILE['name']} - {PROFILE.get('full_name', '')}  v{PROFILE['version']}",
        "Status: Online",
        f"Online since: {STARTED_AT:%d-%b-%Y %H:%M} ({settings.timezone})",
        "",
        "Role:",
        PROFILE["role"],
        f"Authority: {PROFILE['authority']['mode'].replace('_', '-')}",
        "",
        "Available Skills:",
    ]
    for i, s in enumerate(SKILLS.values(), 1):
        lines.append(f"{i}. {s.name}  v{s.version}")
    lines += ["", "Connected Systems:"]
    for sysdef in PROFILE["systems"]:
        kind = sysdef["kind"]
        if kind == "chat":
            ok, note = settings.bot_configured or settings.slash_configured, ""
        elif kind == "ai":
            ok, note = ai_ok, f" ({ai_note})" if ai_note else ""
        elif kind == "storage":
            if nas.configured:
                ok, note = nas.is_dir(settings.ace_share, ["reports"]), f" ({settings.ace_share}/reports)"
            else:
                ok, note = settings.reports_dir.exists(), ""
        else:
            ok, note = False, ""
        lines.append(f"{sysdef['label']} {_tick(ok)}{note}")
    lines += ["", "File access:"]
    if nas.configured:
        ok, note = nas.status()
        lines.append(f"Drive NAS ({settings.nas_host}) as '{settings.nas_user}' {_tick(ok)} - {note}")
    for src in get_sources().values():
        if src.kind == "local":
            lines.append(f"{src.name} (local) {_tick(src.available())}")
    if not settings.allowed_users:
        lines += ["", "⚠️ ALLOWED_USERS is empty - anyone in Chat can use me."]
    return "\n".join(lines)


def help_text() -> str:
    lines = [
        f"{PROFILE['name']} ({PROFILE.get('full_name', '')}) - {PROFILE['role']}",
        "",
        "Commands (in a direct message, or after /ace in a channel):",
        "status - who I am, my skills and connected systems",
        "skills - skill details and versions",
        "ask <question> - ask me a finance/accounting question",
        "files [folder] - browse the client folders I can read, e.g. files Food Box/2026",
    ]
    for s in SKILLS.values():
        lines.append(f"{s.usage} - {s.name}")
    lines += [
        "whoami - show your Chat user id (for permissions)",
        "reset - clear our conversation memory",
        "",
        "Anything else you type is treated as a question.",
    ]
    return "\n".join(lines)


def skills_text() -> str:
    out = ["Skills", "────────────────"]
    for s in SKILLS.values():
        out.append(f"✓ {s.name}  v{s.version}")
        out.append(f"  {s.description}")
        out.append(f"  Use: {s.usage}")
    return "\n".join(out)


def files_text(ref: str = "") -> str:
    try:
        return list_folder(ref)
    except FileAccessError as exc:
        return f"⚠️ {exc}"


def ask(user_key: str, question: str) -> str:
    if not llm.configured:
        return "My AI model is not connected yet (ANTHROPIC_API_KEY missing). Built-in commands still work - send `help`."
    with _history_lock:
        hist = list(_history[user_key])
    skills_list = ", ".join(f"{s.name} (command: {s.usage})" for s in SKILLS.values())
    system = (
        PROFILE["persona"]
        + f"\nToday is {datetime.now(ZoneInfo(settings.timezone)):%A %d %B %Y}."
        + f"\nYour skills: {skills_list}. If the user wants a skill run, tell them the exact command."
    )
    messages = hist + [{"role": "user", "content": question}]
    r = llm.complete(system, messages)
    with _history_lock:
        _history[user_key].append({"role": "user", "content": question})
        _history[user_key].append({"role": "assistant", "content": r.text})
    audit("llm_call", purpose="ask", model=r.model, input_tokens=r.input_tokens, output_tokens=r.output_tokens)
    return r.text


# ---------------------------------------------------------------------------
# Entry point used by every chat integration
# ---------------------------------------------------------------------------

def handle(user_id: str, username: str, text: str, channel: str) -> Response:
    cmd, arg = parse(text)
    who = {"user_id": user_id, "username": username, "channel": channel}

    if not is_allowed(user_id, username):
        audit("denied", command=cmd, **who)
        return Response(f"Sorry {username or 'there'}, you are not authorised to use {PROFILE['name']}. "
                        f"Ask MA to add your user id ({user_id}) to ALLOWED_USERS.")

    audit("request", command=cmd, arg=arg[:200], **who)

    if cmd in ("help", "hi", "hello", "menu", "?"):
        return Response(help_text())
    if cmd == "status":
        return Response("Checking my systems…", background=lambda: status_text())
    if cmd == "skills":
        return Response(skills_text())
    if cmd in ("files", "inbox", "ls"):
        return Response(files_text(arg))
    if cmd == "whoami":
        return Response(f"user_id: {user_id}\nusername: {username}\nauthorised: yes")
    if cmd == "reset":
        with _history_lock:
            _history.pop(user_id, None)
        return Response("Conversation memory cleared.")

    skill = by_command(cmd)
    if skill:
        if not arg:
            return Response(f"Which file? Usage: {skill.usage}\n\n{files_text()}")

        def job() -> str:
            t0 = time.monotonic()
            result = skill.run(arg, requested_by=username or user_id, persona=PROFILE["persona"])
            audit("skill_run", skill=skill.key, version=skill.version, file=arg, ok=result.ok,
                  report=str(result.report_path) if result.report_path else None,
                  seconds=round(time.monotonic() - t0, 1), **who)
            return result.chat_summary

        return Response(f"On it - running {skill.name} v{skill.version} on '{arg}'. I'll message you when the report is ready.",
                        background=job)

    question = text if cmd != "ask" else arg
    question = _PREFIX.sub("", question).strip()
    if not question:
        return Response("What would you like to ask? e.g. `ask Explain EBITDA`")

    def answer() -> str:
        try:
            return ask(user_id, question)
        except Exception as exc:  # noqa: BLE001
            log.exception("ask failed")
            audit("error", where="ask", error=repr(exc), **who)
            return f"⚠️ I couldn't reach the AI model ({type(exc).__name__}). Please try again shortly."

    return Response("", background=answer)
