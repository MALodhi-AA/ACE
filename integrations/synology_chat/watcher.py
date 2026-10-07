"""Channel watcher (v0.4): ACE as a member of Synology Chat channels.

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
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.audit import audit
from app.config import settings
from app.storage import channel_files_store, free_name
from integrations.synology_chat.client import plain
from integrations.synology_chat.user_client import ChatError, ChatUser

log = logging.getLogger(__name__)

WATCHED_TYPES = {"public", "private"}
PAGE = 100
MAX_ATTEMPTS = 3
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


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
        self.stats = {"running": False, "channels": 0, "last_check": None, "saved_today": 0,
                      "day": None, "last_error": ""}
        self._stop = threading.Event()

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
        return safe_name(ch.get("name") or "", f"channel-{cid}")

    def watched(self, channels: list[dict]) -> list[dict]:
        ignore = settings.chat_ignore
        out = []
        for ch in channels:
            if ch.get("type") not in WATCHED_TYPES or ch.get("is_joined") is False:
                continue
            if str(ch.get("channel_id")) in ignore or str(ch.get("name", "")).lower() in ignore:
                continue
            out.append(ch)
        return out

    # --- one polling round -----------------------------------------------------
    def poll_once(self) -> int:
        """Check all watched channels once. Returns the number of files saved."""
        saved = 0
        channels = self.watched(self.chat.channels())
        self.stats["channels"] = len(channels)
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
                audit("channel_watch_start", channel_id=cid, channel=self.folder_name(ch), from_post=latest)
                continue
            if ch.get("last_post_at", 0) and ch.get("last_post_at", 0) <= st.get("last_at", 0):
                continue  # nothing new
            saved += self._catch_up(ch, st)
            st["last_at"] = max(st.get("last_at", 0), ch.get("last_post_at", 0))
            self._save()
        now = datetime.now(self.tz)
        if self.stats["day"] != now.date():
            self.stats["day"], self.stats["saved_today"] = now.date(), 0
        self.stats["saved_today"] += saved
        self.stats["last_check"] = now
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
        fp = post.get("file_props")
        if post.get("type") != "file" or not fp or post.get("delete_at"):
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
        return "Channel files: off (CHAT_WATCH=false)"
    w = watcher
    if not w or not w.stats["last_check"]:
        err = f" - {w.stats['last_error']}" if w and w.stats["last_error"] else ""
        return f"Channel files: starting ✗{err}" if err else "Channel files: starting..."
    s = w.stats
    ok = "✓" if not s["last_error"] else "⚠️"
    line = (f"Channel files {ok} - watching {s['channels']} channels as '{settings.chat_user}', "
            f"{s['saved_today']} saved today, last check {s['last_check']:%H:%M}")
    if s["last_error"]:
        line += f" ({s['last_error']})"
    return line
