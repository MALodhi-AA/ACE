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
RECENT_ROOTS = 30          # threads on the last N messages of a channel are watched for replies
THREAD_SCAN_EVERY = 2      # polls between thread scans
MAX_ATTEMPTS = 3
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MENTION_TOKEN = re.compile(r"@u:\d+\s*")
_ACE_NAME = re.compile(r"^\s*@?ace\b[:,]?\s*", re.IGNORECASE)


_COLLECT = re.compile(r"^(?:please\s+)?(?:collect|save|fetch|download)\s+(?:all\s+)?(?:the\s+)?(?:old\s+)?files?\b(.*)$",
                      re.IGNORECASE | re.DOTALL)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b"), "dmy"),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON}\.?,?\s+(\d{{4}})\b", re.I), "d_mon_y"),
    (re.compile(rf"\b{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "mon_d_y"),
]
_MONTH_YEAR = [re.compile(rf"\b{_MON}\.?,?\s+(\d{{4}})\b", re.I), re.compile(r"\b(\d{4})-(\d{1,2})\b(?!-)"),
               re.compile(r"\b(\d{1,2})/(\d{4})\b")]


def _mon(name: str) -> int:
    return _MONTHS[name.lower()[:3]]


def parse_date(text: str):
    """One date in any common form (2026-09-01, 01/09/2026, 1 Sep 2026, Sep 1 2026); None if not a date."""
    from datetime import date
    for rx, form in _DATE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        g = m.groups()
        try:
            if form == "ymd":
                return date(int(g[0]), int(g[1]), int(g[2]))
            if form == "dmy":
                return date(int(g[2]), int(g[1]), int(g[0]))
            if form == "d_mon_y":
                return date(int(g[2]), _mon(g[1]), int(g[0]))
            return date(int(g[2]), _mon(g[0]), int(g[1]))
        except ValueError:
            return None
    return None


def _month_span(year: int, month: int):
    from calendar import monthrange
    from datetime import date
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def parse_period(text: str, today=None):
    """Find a period in free text -> (from, to, rest_of_text). Raises ValueError for an impossible date."""
    from datetime import date, timedelta
    today = today or date.today()
    rest = " " + text + " "
    found = []                                   # (position, date, keyword-before)
    for rx, _form in _DATE_PATTERNS:
        for m in list(rx.finditer(rest)):
            d = parse_date(m.group(0))
            if d is None:
                raise ValueError(f"I couldn't read the date '{m.group(0).strip()}'. Try e.g. 2026-09-01 or 1 Sep 2026.")
            before = rest[max(0, m.start() - 12):m.start()].lower()
            found.append((m.start(), d, before))
            rest = rest[:m.start()] + " " * (m.end() - m.start()) + rest[m.end():]
    found.sort()
    if len(found) >= 2:
        lo, hi = found[0][1], found[-1][1]
        return min(lo, hi), max(lo, hi), rest
    if len(found) == 1:
        _, d, before = found[0]
        if re.search(r"\b(from|since|after)\s*$", before):
            return d, None, rest
        if re.search(r"\b(to|until|till|up\s+to|before)\s*$", before):
            return None, d, rest
        return d, d, rest                        # a single day ("on 5 Oct 2026")
    for rx in _MONTH_YEAR:                       # whole month: "Oct 2026", "2026-10", "10/2026"
        m = rx.search(rest)
        if m:
            g = m.groups()
            try:
                if rx is _MONTH_YEAR[0]:
                    y, mo = int(g[1]), _mon(g[0])
                elif rx is _MONTH_YEAR[1]:
                    y, mo = int(g[0]), int(g[1])
                else:
                    y, mo = int(g[1]), int(g[0])
                lo, hi = _month_span(y, mo)
            except (ValueError, KeyError):
                continue
            return lo, hi, rest[:m.start()] + " " + rest[m.end():]
    low = rest.lower()
    if "yesterday" in low:
        d = today - timedelta(days=1)
        return d, d, low.replace("yesterday", " ")
    if "today" in low:
        return today, today, low.replace("today", " ")
    if "last month" in low or "previous month" in low:
        first = today.replace(day=1) - timedelta(days=1)
        lo, hi = _month_span(first.year, first.month)
        return lo, hi, re.sub(r"(last|previous) month", " ", low)
    if "this month" in low:
        return today.replace(day=1), today, low.replace("this month", " ")
    if "this year" in low:
        return date(today.year, 1, 1), today, low.replace("this year", " ")
    return None, None, rest


def parse_collect(text: str, today=None) -> dict | None:
    """'collect files ...' in plain words -> {'text': words left (may name a channel), 'from', 'to'}.
    None if the message is not a collect instruction. Raises ValueError for a bad date."""
    m = _COLLECT.match(text.strip())
    if not m:
        return None
    lo, hi, rest = parse_period(m.group(1), today)
    quoted = re.findall(r"[\"'“”‘’]([^\"'“”‘’]{2,})[\"'“”‘’]", m.group(1))
    return {"text": rest, "quoted": quoted[0].strip() if quoted else "", "from": lo, "to": hi}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def day_folder(create_at_ms: int, tz) -> list[str]:
    """Year / year-month / date, e.g. ['2026', '2026-10', '2026-10-07']."""
    d = datetime.fromtimestamp((create_at_ms or 0) / 1000, tz)
    return [d.strftime("%Y"), d.strftime("%Y-%m"), d.strftime("%Y-%m-%d")]


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
        self._polls = 0
        self._saved: dict[int, set[int]] = {}       # channel -> post ids already saved
        self._channels: list[dict] = []
        self._collecting = threading.Lock()
        me = settings.chat_user_id or self.state.get("_me", {}).get("id")
        if me and getattr(self.chat, "me", None) is None:
            self.chat.me = int(me)

    # --- state -----------------------------------------------------------------
    STATE_VERSION = 3

    def _load(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"_v": self.STATE_VERSION}
        if state.get("_v") != self.STATE_VERSION:
            # Before v0.4.8 the starting point of some channels was found wrongly (Chat caps
            # page sizes; some channels answer nothing without an anchor). Start every chat again from its newest message so
            # nothing old is answered or saved by itself.
            log.info("channel watcher: state from an older version - starting from the newest messages")
            state = {k: v for k, v in state.items() if k.startswith("_")}
            state["_v"] = self.STATE_VERSION
        return state

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
        self._channels = channels
        self.stats["channels"] = sum(1 for c in channels if kind(c) != "direct")
        self.stats["direct"] = len(channels) - self.stats["channels"]
        for ch in channels:
            cid = int(ch["channel_id"])
            key = str(cid)
            st = self.state.get(key)
            if st is None:
                # empty chat: start before message 1 so its first message is handled
                latest = self.chat.latest_post_id(cid) or (cid << 32)
                self.state[key] = {"last_id": latest, "last_at": ch.get("last_post_at", 0),
                                   "name": self.folder_name(ch)}
                self._save()
                log.info("watching channel %s (%s) from post %s", cid, self.folder_name(ch), latest)
                audit("channel_watch_start", channel_id=cid, channel=self.folder_name(ch), type=ch.get("type"),
                      from_post=latest)
                continue
            try:
                if not (ch.get("last_post_at", 0) and ch.get("last_post_at", 0) <= st.get("last_at", 0)):
                    saved += self._catch_up(ch, st)
                    st["last_at"] = max(st.get("last_at", 0), ch.get("last_post_at", 0))
                    self._save()
                if self._polls % THREAD_SCAN_EVERY == 0:
                    saved += self._scan_threads(ch, st)
            except ChatError as exc:
                # one chat's problem must not stop the others
                log.warning("channel %s (%s): %s", cid, self.folder_name(ch), exc)
        self._polls += 1
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
            anchor = max(st["last_id"], (cid << 32) + 1)       # Chat needs an anchor of 1 or more
            try:
                batch = self.chat.posts(cid, anchor, next_count=PAGE)
            except ChatError as exc:
                if exc.code == 402:                           # "post not found": chat still empty
                    return saved
                raise
            newer = sorted((p for p in batch if p.get("post_id", 0) > st["last_id"]), key=lambda p: p["post_id"])
            if not newer:
                return saved
            for post in newer:
                done, ok = self._handle(ch, post)
                if not done:
                    self._save()
                    return saved  # retry this post next round
                saved += ok
                st["last_id"] = post["post_id"]
            self._save()      # loop again: Chat may return fewer than asked even when more exist

    # --- thread replies ---------------------------------------------------------------
    def _scan_threads(self, ch: dict, st: dict) -> int:
        """Look for new replies in threads on the channel's recent messages.

        Chat keeps replies out of the normal post list. A message with replies has
        thread_id == its own post_id and a `last_comment_at` time; its replies are listed
        with thread_id=<message> and an anchor before the replies.
        """
        cid = int(ch["channel_id"])
        if st.get("last_id", 0) <= (cid << 32):
            return 0
        recent = sorted(self.chat.posts(cid, st["last_id"], prev_count=RECENT_ROOTS),
                        key=lambda p: p["post_id"])
        threads = st.setdefault("threads", {})
        first_scan = not st.get("threads_ready")
        saved = 0
        for i, root in enumerate(recent):
            rid = root["post_id"]
            if root.get("thread_id") != rid or not root.get("last_comment_at"):
                continue
            th = threads.get(str(rid))
            if th is None:
                # Threads that already had replies when ACE first looked: start from now.
                start = root["last_comment_at"] if first_scan else 0
                # `since`: replies at or before this time are old and never handled
                th = threads[str(rid)] = {"seen_at": start, "since": start, "last_id": 0}
            if root["last_comment_at"] <= th["seen_at"]:
                continue
            anchor = th["last_id"] or (recent[i - 1]["post_id"] if i > 0 else rid - 1)
            done = True
            while True:
                replies = sorted((p for p in self.chat.posts(cid, anchor, next_count=PAGE, thread_id=rid)
                                  if p.get("thread_id") == rid and p["post_id"] != rid
                                  and p["post_id"] > th["last_id"]
                                  and p.get("create_at", 0) > th.get("since", 0)), key=lambda p: p["post_id"])
                for post in replies:
                    finished, ok = self._handle(ch, post)
                    if not finished:
                        done = False
                        break
                    saved += ok
                    th["last_id"] = anchor = post["post_id"]
                if not done or not replies:
                    break
            if done:
                th["seen_at"] = root["last_comment_at"]
            self._save()
        st["threads_ready"] = True
        # forget threads that dropped out of the recent window
        keep = {str(p["post_id"]) for p in recent}
        for k in [k for k in threads if k not in keep]:
            threads.pop(k)
        self._save()
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
        thread = post.get("thread_id") or pid
        original = fp.get("name") or f"file_{pid}"
        size = int(fp.get("size") or 0)
        folder_name = self.folder_name(ch)
        if pid in self._saved_ids(cid):
            return True, 0                       # already collected
        if size > settings.chat_max_file_mb * 1024 * 1024:
            audit("channel_file_skipped", channel=folder_name, post_id=pid, file=original, size=size,
                  reason="too large")
            self._reply(cid, thread, f"Not saved: {original} is larger than {settings.chat_max_file_mb} MB.")
            return True, 0
        try:
            name, where = self.store_file(ch, post)
        except Exception as exc:  # noqa: BLE001
            n = self.attempts.get(pid, 0) + 1
            self.attempts[pid] = n
            self.stats["last_error"] = f"{original}: {exc}"
            log.warning("saving %s from channel %s failed (attempt %s): %s", original, folder_name, n, exc)
            if n < MAX_ATTEMPTS:
                return False, 0
            audit("channel_file_failed", channel=folder_name, post_id=pid, file=original, error=str(exc))
            self._reply(cid, thread, f"I couldn't save {original}: {exc}")
            self.attempts.pop(pid, None)
            return True, 0
        self.attempts.pop(pid, None)
        self._reply(cid, thread, f"Saved: {name} -> {where.replace(chr(92), '/')}")
        return True, 1

    # --- saving files -------------------------------------------------------------------
    def _saved_path(self, cid: int):
        return self.state_path.parent / "saved" / f"{cid}.txt"

    def _saved_ids(self, cid: int) -> set[int]:
        if cid not in self._saved:
            ids: set[int] = set()
            try:
                ids = {int(x) for x in self._saved_path(cid).read_text().split() if x.isdigit()}
            except OSError:
                pass
            self._saved[cid] = ids
        return self._saved[cid]

    def _mark_saved(self, cid: int, pid: int) -> None:
        self._saved_ids(cid).add(pid)
        try:
            path = self._saved_path(cid)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{pid}\n")
        except OSError as exc:
            log.warning("could not record saved post: %s", exc)

    def store_file(self, ch: dict, post: dict, how: str = "live") -> tuple[str, str]:
        """Download a shared file and save it to <Channel>/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/ (never overwrites)."""
        fp = post.get("file_props") or {}
        pid = post["post_id"]
        cid = int(ch["channel_id"])
        original = fp.get("name") or f"file_{pid}"
        size = int(fp.get("size") or 0)
        folder = [self.folder_name(ch)] + day_folder(post.get("create_at", 0), self.tz)
        data = self.chat.download(pid)
        if size and len(data) != size:
            raise ChatError(f"incomplete download ({len(data)} of {size} bytes)")
        name = free_name(self.store, folder, safe_name(original, f"file_{pid}"))
        where = self.store.save(folder + [name], data)
        self._mark_saved(cid, pid)
        audit("channel_file_saved", channel=folder[0], channel_id=cid, post_id=pid, file=original,
              saved_as=where, size=len(data), shared_by=post.get("creator_id"), how=how)
        log.info("saved %s -> %s (%s)", original, where, how)
        return name, where

    # --- collecting old files (manager instruction) -------------------------------
    def is_admin(self, user_id) -> bool:
        uid = str(user_id)
        return any(a.lower() in {uid.lower(), self.username(uid).lower()} for a in settings.ace_admins)

    def find_channel(self, name: str) -> dict | None:
        """Match a channel name loosely: case, spaces, dashes and dots don't matter."""
        want = _norm(name)
        if not want:
            return None
        for ch in self._channels:
            if want in {str(ch.get("channel_id")), _norm(ch.get("name") or ""), _norm(self.folder_name(ch))}:
                return ch
        return None

    def channel_in_text(self, text: str) -> dict | None:
        """The channel whose name appears in a sentence (longest match wins)."""
        flat = _norm(text)
        best = None
        for ch in self._channels:
            if kind(ch) == "direct":
                continue
            for n in {_norm(ch.get("name") or ""), _norm(self.folder_name(ch))}:
                if len(n) >= 4 and n in flat and (best is None or len(n) > best[0]):
                    best = (len(n), ch)
        return best[1] if best else None

    def collect(self, ch: dict, start=None, end=None) -> dict:
        """Save every file shared in a channel (main messages and thread replies) between two dates."""
        cid = int(ch["channel_id"])
        lo = int(datetime.combine(start, datetime.min.time(), self.tz).timestamp() * 1000) if start else 0
        hi = (int(datetime.combine(end, datetime.max.time(), self.tz).timestamp() * 1000) if end else 1 << 62)
        res = {"saved": 0, "already": 0, "skipped": 0, "failed": 0, "bytes": 0, "errors": [],
               "checked": 0, "first_at": 0, "last_at": 0}

        def take(post: dict) -> None:
            fp = post.get("file_props")
            if post.get("type") != "file" or not fp or post.get("delete_at"):
                return
            if not lo <= post.get("create_at", 0) <= hi:
                return
            if post["post_id"] in self._saved_ids(cid):
                res["already"] += 1
                return
            if int(fp.get("size") or 0) > settings.chat_max_file_mb * 1024 * 1024:
                res["skipped"] += 1
                return
            try:
                self.store_file(ch, post, how="collect")
                res["saved"] += 1
                res["bytes"] += int(fp.get("size") or 0)
            except Exception as exc:  # noqa: BLE001
                res["failed"] += 1
                if len(res["errors"]) < 5:
                    res["errors"].append(f"{fp.get('name')}: {exc}")

        first = self.chat.first_post(cid)
        if not first:
            return res
        prev_id, anchor, first_page = first["post_id"] - 1, first["post_id"], True
        while True:
            page = sorted((p for p in self.chat.posts(cid, anchor, next_count=PAGE)
                           if first_page or p["post_id"] > anchor), key=lambda p: p["post_id"])
            first_page = False
            if not page:
                break
            for p in page:
                res["checked"] += 1
                res["first_at"] = res["first_at"] or p.get("create_at", 0)
                res["last_at"] = max(res["last_at"], p.get("create_at", 0))
                if p.get("create_at", 0) > hi:
                    return res
                take(p)
                if p.get("comment_count") and p.get("last_comment_at", 0) >= lo:
                    rid, after = p["post_id"], prev_id
                    while True:
                        replies = sorted((r for r in self.chat.posts(cid, after, next_count=PAGE, thread_id=rid)
                                          if r.get("thread_id") == rid and r["post_id"] not in (rid,)
                                          and r["post_id"] > after), key=lambda r: r["post_id"])
                        for r in replies:
                            take(r)
                            after = r["post_id"]
                        if not replies:
                            break
                prev_id = p["post_id"]
            anchor = page[-1]["post_id"]
        return res

    def _start_collect(self, ch_here: dict, post: dict, cmd: dict) -> None:
        cid = int(ch_here["channel_id"])
        thread = post.get("thread_id") or None
        uid = post.get("creator_id")
        if not self.is_admin(uid):
            audit("denied", command="collect files", user_id=uid)
            from app.profile import MANAGER
            self.say(cid, f"Only {MANAGER} can ask me to collect old files.", thread)
            return
        target = self.find_channel(cmd["quoted"]) if cmd["quoted"] else None
        target = target or self.channel_in_text(cmd["text"]) or (self.channel_in_text(cmd["quoted"]) if cmd["quoted"] else None)
        if target is None:
            if cmd["quoted"] or kind(ch_here) == "direct":
                import difflib
                names = sorted(self.folder_name(c) for c in self._channels if kind(c) != "direct")
                guess = difflib.get_close_matches(cmd["quoted"] or cmd["text"].strip(), names, n=1, cutoff=0.5)
                hint = f" Did you mean {guess[0]}?" if guess else ""
                what = f"'{cmd['quoted']}'" if cmd["quoted"] else "that"
                self.say(cid, f"I'm not in a channel called {what}.{hint} I'm in: {', '.join(names)}", thread)
                return
            target = ch_here
        span = (f" from {cmd['from']:%d-%b-%Y}" if cmd["from"] else "") + (f" to {cmd['to']:%d-%b-%Y}" if cmd["to"] else "")
        if not self._collecting.acquire(blocking=False):
            self.say(cid, "I'm already collecting files - I'll finish that first.", thread)
            return
        name = self.folder_name(target)
        audit("collect_start", channel=name, by=uid, start=str(cmd["from"]), end=str(cmd["to"]))
        self.say(cid, f"On it - collecting all files shared in {name}{span or ' (all dates)'}, "
                      f"including threads. I'll tell you when I'm done.", thread)

        def run() -> None:
            try:
                r = self.collect(target, cmd["from"], cmd["to"])
                mb = r["bytes"] / 1024 / 1024
                lines = [f"Done: {r['saved']} files collected from {name}{span} ({mb:.1f} MB).",
                         f"Saved in ACE/channel-files/{name}/<year>/<month>/<day>/"]
                if r["checked"]:
                    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, self.tz).strftime("%d-%b-%Y")  # noqa: E731
                    lines.append(f"I looked through {r['checked']} messages dated {fmt(r['first_at'])} "
                                 f"to {fmt(r['last_at'])}, plus their threads.")
                else:
                    lines.append("I couldn't read any messages in that channel.")
                if r["already"]:
                    lines.append(f"{r['already']} were already saved earlier (not duplicated).")
                if r["skipped"]:
                    lines.append(f"{r['skipped']} skipped - larger than {settings.chat_max_file_mb} MB.")
                if r["failed"]:
                    lines.append(f"{r['failed']} could not be saved:")
                    lines += [f"- {e}" for e in r["errors"]]
                audit("collect_done", channel=name, **{k: v for k, v in r.items() if k != "errors"})
                log.info("collect %s: %s", name, {k: v for k, v in r.items() if k != "errors"})
                self.stats["saved_today"] += r["saved"]
                self.say(cid, "\n".join(lines), thread)
            except Exception as exc:  # noqa: BLE001
                log.exception("collect failed")
                self.say(cid, f"Collecting stopped with an error: {exc}", thread)
            finally:
                self._collecting.release()

        self._pool.submit(run)

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
            cmd = parse_collect(text, datetime.now(self.tz).date())
        except ValueError as exc:
            self.say(cid, str(exc), thread)
            return
        if cmd is not None:
            self._start_collect(ch, post, cmd)
            return
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
