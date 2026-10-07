"""Channel watcher (v0.4): ACE as a member of Synology Chat channels.

v0.4.1: ACE also *talks* as a team member - it answers every direct message
sent to its Chat account and every message in a channel that mentions @ACE,
with the same commands as the bot (status, ask, files, mis ...).

Every CHAT_POLL_SECONDS ACE looks at the channels its Chat account belongs to.
Each file shared there is downloaded and saved, never overwritten, to

    ACE/channel-files/<Channel>/<YYYY-MM>/<original file name>

and ACE replies under the file with where it was saved.

- Only new posts are handled: the first time ACE sees a channel it notes the
  newest post and starts from there (no back-filling of old files).
- Progress is kept in STATE_DIR/chat_watch.json, so after a restart ACE
  catches up on files shared while it was offline.
- A file that cannot be saved is retried; after 3 failed attempts ACE says so
  in the channel and moves on.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from app.audit import audit
from app.config import settings
from app.storage import channel_files_store, free_name
from integrations.synology_chat.client import chunk_text, plain
from integrations.synology_chat.user_client import ChatError, ChatUser

log = logging.getLogger(__name__)

CHANNEL_TYPES = {"public", "private"}
SKIP_TYPES = {"synobot", "chatbot", "bot"}
# Synology Chat lists unnamed conversations (one-to-one and small group chats) as
# type "anonymous": 2 members = direct chat with ACE, more = group conversation.
PAGE = 100
MAX_ATTEMPTS = 3
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MENTION_TOKEN = re.compile(r"@u:\d+\s*")
_ACE_NAME = re.compile(r"^\s*@?ace\b[:,]?\s*", re.IGNORECASE)


def kind(ch: dict) -> str | None:
    """'channel', 'direct' (one-to-one with ACE), 'group' (conversation) or None (not handled)."""
    t = str(ch.get("type", "")).lower()
    if t in CHANNEL_TYPES:
        return "channel"
    if t in SKIP_TYPES or not t:
        return None
    members = ch.get("total_member_count")
    if members is None or int(members) <= 2:
        return "direct"
    return "group"


def mentions(post: dict, me: int | None) -> bool:
    text = str(post.get("message") or "")
    if me is not None:
        for m in post.get("mentions") or []:
            if str(m).split(":")[-1] == str(me):
                return True
        if re.search(rf"@u:{me}\b", text):
            return True
    return bool(re.search(r"(^|\s)@ace\b", text, re.IGNORECASE))


def clean_text(text: str) -> str:
    return _ACE_NAME.sub("", _MENTION_TOKEN.sub("", text or "")).strip()


def safe_name(name: str, fallback: str = "file") -> str:
    name = _BAD_CHARS.sub("_", str(name or "")).strip().strip(".").strip()
    return name[:150] or fallback


class ChannelWatcher:
    def __init__(self, chat: ChatUser, store=None, state_path=None, tz: str | None = None):
        self.chat = chat
        self.store = store or channel_files_store()
        self.state_path = state_path or (settings.state_dir / "chat_watch.json")
        self.tz = ZoneInfo(tz or settings.timezone)
        self.state: dict[str, dict] = self._load()
        self.attempts: dict[int, int] = {}
        self.stats = {"running": False, "channels": 0, "direct": 0, "last_check": None, "saved_today": 0,
                      "answered_today": 0, "day": None, "last_error": ""}
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ace-chat-job")
        self._users: dict[int, str] = {}
        me = settings.chat_user_id or self.state.get("_me", {}).get("id")
        if me and getattr(self.chat, "me", None) is None:
            self.chat.me = int(me)

    # --- state -----------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("could not save watcher state: %s", exc)

    # --- helpers ---------------------------------------------------------------
    def folder_name(self, ch: dict) -> str:
        cid = str(ch.get("channel_id"))
        names = settings.chat_channel_names
        if cid in names:
            return safe_name(names[cid])
        if kind(ch) in ("direct", "group"):
            prefix = "Direct" if kind(ch) == "direct" else "Group"
            return safe_name(f"{prefix}-{ch.get('name') or cid}", f"{prefix}-{cid}")
        return safe_name(ch.get("name") or "", f"channel-{cid}")

    def username(self, user_id) -> str:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return ""
        if uid not in self._users:
            try:
                self._users = self.chat.users() or self._users
            except Exception:  # noqa: BLE001
                pass
            self._users.setdefault(uid, "")
        return self._users.get(uid, "")

    def watched(self, channels: list[dict]) -> list[dict]:
        ignore = settings.chat_ignore
        out = []
        for ch in channels:
            if kind(ch) is None or ch.get("is_joined") is False:
                continue
            if str(ch.get("channel_id")) in ignore or str(ch.get("name", "")).lower() in ignore:
                continue
            out.append(ch)
        return out

    # --- one polling round -----------------------------------------------------
    def poll_once(self) -> int:
        """Check all watched channels once. Returns the number of files saved."""
        saved = 0
        today = datetime.now(self.tz).date()
        if self.stats["day"] != today:
            self.stats.update(day=today, saved_today=0, answered_today=0)
        channels = self.watched(self.chat.channels())
        self.stats["channels"] = sum(1 for c in channels if kind(c) != "direct")
        self.stats["direct"] = len(channels) - self.stats["channels"]
        for ch in channels:
            cid = int(ch["channel_id"])
            key = str(cid)
            st = self.state.get(key)
            if st is None:
                latest = self.chat.latest_post_id(cid)
                self.state[key] = {"last_id": latest, "last_at": ch.get("last_post_at", 0),
                                   "name": self.folder_name(ch)}
                self._save()
                log.info("watching channel %s (%s) from post %s", cid, self.folder_name(ch), latest)
                audit("channel_watch_start", channel_id=cid, channel=self.folder_name(ch), type=ch.get("type"),
                      from_post=latest)
                continue
            if ch.get("last_post_at", 0) and ch.get("last_post_at", 0) <= st.get("last_at", 0):
                continue  # nothing new
            saved += self._catch_up(ch, st)
            st["last_at"] = max(st.get("last_at", 0), ch.get("last_post_at", 0))
            self._save()
        now = datetime.now(self.tz)
        self.stats["saved_today"] += saved
        self.stats["last_check"] = now
        if getattr(self.chat, "me", None) and self.state.get("_me", {}).get("id") != self.chat.me:
            self.state["_me"] = {"id": self.chat.me}
            self._save()
        return saved

    def _catch_up(self, ch: dict, st: dict) -> int:
        cid = int(ch["channel_id"])
        saved = 0
        while True:
            newer = sorted((p for p in self.chat.posts(cid, st["last_id"], next_count=PAGE)
                            if p.get("post_id", 0) > st["last_id"]), key=lambda p: p["post_id"])
            if not newer:
                return saved
            for post in newer:
                done, ok = self._handle(ch, post)
                if not done:
                    self._save()
                    return saved  # retry this post next round
                saved += ok
                st["last_id"] = post["post_id"]
            self._save()
            if len(newer) < PAGE:
                return saved

    def _handle(self, ch: dict, post: dict) -> tuple[bool, int]:
        """Returns (finished_with_post, files_saved)."""
        if post.get("delete_at") or post.get("type") == "system":
            return True, 0
        me = getattr(self.chat, "me", None)
        if me is not None and post.get("creator_id") == me:
            return True, 0                       # ACE's own posts
        fp = post.get("file_props")
        if post.get("type") != "file" or not fp:
            if post.get("message") and (kind(ch) == "direct" or mentions(post, me)):
                self._converse(ch, post)
            return True, 0
        pid = post["post_id"]
        cid = int(ch["channel_id"])
        original = fp.get("name") or f"file_{pid}"
        size = int(fp.get("size") or 0)
        folder_name = self.folder_name(ch)
        if size > settings.chat_max_file_mb * 1024 * 1024:
            audit("channel_file_skipped", channel=folder_name, post_id=pid, file=original, size=size,
                  reason="too large")
            self._reply(cid, pid, f"Not saved: {original} is larger than {settings.chat_max_file_mb} MB.")
            return True, 0
        month = datetime.fromtimestamp(post.get("create_at", 0) / 1000, self.tz).strftime("%Y-%m")
        folder = [folder_name, month]
        try:
            data = self.chat.download(pid)
            if size and len(data) != size:
                raise ChatError(f"incomplete download ({len(data)} of {size} bytes)")
            name = free_name(self.store, folder, safe_name(original, f"file_{pid}"))
            where = self.store.save(folder + [name], data)
        except Exception as exc:  # noqa: BLE001
            n = self.attempts.get(pid, 0) + 1
            self.attempts[pid] = n
            self.stats["last_error"] = f"{original}: {exc}"
            log.warning("saving %s from channel %s failed (attempt %s): %s", original, folder_name, n, exc)
            if n < MAX_ATTEMPTS:
                return False, 0
            audit("channel_file_failed", channel=folder_name, post_id=pid, file=original, error=str(exc))
            self._reply(cid, pid, f"I couldn't save {original}: {exc}")
            self.attempts.pop(pid, None)
            return True, 0
        self.attempts.pop(pid, None)
        audit("channel_file_saved", channel=folder_name, channel_id=cid, post_id=pid, file=original,
              saved_as=where, size=len(data), shared_by=post.get("creator_id"))
        log.info("saved %s -> %s", original, where)
        self._reply(cid, pid, f"Saved: {name} -> {where.replace(chr(92), '/')}")
        return True, 1

    # --- conversation ---------------------------------------------------------------
    def _converse(self, ch: dict, post: dict) -> None:
        """Answer a direct message or an @ACE mention, like the bot does."""
        from app.employee import handle   # late import: employee imports this module for status

        cid = int(ch["channel_id"])
        uid = str(post.get("creator_id", ""))
        text = clean_text(post.get("message", ""))
        where = "chat-direct" if kind(ch) == "direct" else f"chat-{kind(ch)}:{self.folder_name(ch)}"
        thread = post.get("thread_id") or None
        self.stats["answered_today"] += 1
        try:
            resp = handle(uid, self.username(uid), text or "help", where)
        except Exception as exc:  # noqa: BLE001
            log.exception("handling chat message failed")
            audit("error", where=where, error=repr(exc), user_id=uid)
            self.say(cid, f"Something went wrong ({type(exc).__name__}). The error has been logged.", thread)
            return
        if resp.text:
            self.say(cid, resp.text, thread)
        if resp.background:
            job = resp.background

            def run() -> None:
                try:
                    out = job()
                except Exception as exc:  # noqa: BLE001
                    log.exception("background job failed")
                    audit("error", where=where, error=repr(exc), user_id=uid)
                    out = f"Something went wrong ({type(exc).__name__}). The error has been logged."
                if out:
                    self.say(cid, out, thread)

            self._pool.submit(run)

    def say(self, cid: int, text: str, thread_id: int | None = None) -> None:
        for part in chunk_text(plain(text)):
            try:
                self.chat.send(cid, part, thread_id=thread_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("message to channel %s failed: %s", cid, exc)
                return

    def _reply(self, cid: int, pid: int, text: str) -> None:
        if not settings.chat_reply_on_save:
            return
        try:
            self.chat.send(cid, plain(text), thread_id=pid)
        except Exception as exc:  # noqa: BLE001
            log.warning("reply in channel %s failed: %s", cid, exc)

    # --- background loop -----------------------------------------------------------
    def run_forever(self) -> None:
        self.stats["running"] = True
        backoff = settings.chat_poll_seconds
        while not self._stop.is_set():
            try:
                self.poll_once()
                self.stats["last_error"] = self.stats["last_error"] if self.attempts else ""
                backoff = settings.chat_poll_seconds
            except Exception as exc:  # noqa: BLE001
                self.stats["last_error"] = str(exc)
                log.warning("channel watcher: %s", exc)
                backoff = min(backoff * 2, 300)
            self._stop.wait(backoff)
        self.stats["running"] = False

    def stop(self) -> None:
        self._stop.set()


watcher: ChannelWatcher | None = None


def start() -> ChannelWatcher | None:
    """Start the watcher thread if ACE's Chat account is configured."""
    global watcher
    if watcher or not (settings.chat_user_configured and settings.chat_watch):
        return watcher
    chat = ChatUser(settings.synology_base_url, settings.chat_user, settings.chat_password,
                    verify=settings.synology_verify_ssl)
    watcher = ChannelWatcher(chat)
    threading.Thread(target=watcher.run_forever, name="chat-watcher", daemon=True).start()
    log.info("channel watcher started (every %ss)", settings.chat_poll_seconds)
    return watcher


def status_line() -> str | None:
    if not settings.chat_user_configured:
        return None
    if not settings.chat_watch:
        return "Chat account: off (CHAT_WATCH=false)"
    w = watcher
    if not w or not w.stats["last_check"]:
        err = f" - {w.stats['last_error']}" if w and w.stats["last_error"] else ""
        return f"Chat account: starting ✗{err}" if err else "Chat account: starting..."
    s = w.stats
    ok = "✓" if not s["last_error"] else "⚠️"
    line = (f"Chat account '{settings.chat_user}' {ok} - in {s['channels']} channels/groups and {s['direct']} direct chats; "
            f"today {s['saved_today']} files saved, {s['answered_today']} messages answered; "
            f"last check {s['last_check']:%H:%M}")
    if s["last_error"]:
        line += f" ({s['last_error']})"
    return line
