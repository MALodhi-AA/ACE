"""Synology Chat as a *user* (ACE's own Chat account), not as a bot.

Uses the same web API the Chat web page uses (verified on the DS723+ with the
v0.3.2 probe):

  SYNO.Chat.Channel list  v5            channels ACE is a member of
  SYNO.Chat.Post    list  v8            post_id=<anchor>, next_count / prev_count
                                         (without an anchor it returns the oldest post)
  SYNO.Chat.Post    create v8           channel_id, message (plain text)
  SYNO.Chat.Post.File get v2            post_id -> the shared file's bytes

Post ids are (channel_id << 32) + running number, so they only ever grow
within a channel.

This API is not officially documented by Synology; a DSM/Chat update may
need small adjustments here.
"""
from __future__ import annotations

import logging
import threading

import httpx

log = logging.getLogger(__name__)

SESSION_ERRORS = {105, 106, 107, 119}   # not logged in / timed out / duplicate login / sid invalid
AUTH_ERRORS = {
    400: "wrong user name or password",
    401: "account disabled",
    402: "permission denied - allow the Synology Chat application for this user",
    403: "2-step verification is required for this account - turn it off for the ace Chat user",
    404: "2-step verification code failed",
    406: "2-step verification must be enabled (DSM policy) - exempt the ace user",
    407: "the HP server's IP is blocked by DSM auto-block - unblock it in Security > Protection",
    408: "password expired - sign in once in a browser and set a new one",
    409: "password expired - sign in once in a browser and set a new one",
    410: "password must be changed - sign in once in a browser and set a new one",
}


class ChatError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class ChatUser:
    """A signed-in Synology Chat user session (thread-safe, re-signs in when needed)."""

    POST_VERSION = 8
    CHANNEL_VERSION = 5
    FILE_VERSION = 2

    def __init__(self, base: str, user: str, password: str, verify: bool = True, timeout: float = 60):
        self.base = base.rstrip("/")
        self.user, self._password = user, password
        self.http = httpx.Client(timeout=timeout, verify=verify)
        self.sid = ""
        self.syno_token = ""
        self.apis: dict[str, dict] = {}
        self.me: int | None = None          # ACE's own Chat user id (learnt from its posts)
        self._lock = threading.RLock()

    # --- low level -----------------------------------------------------------
    def raw(self, api: str, method: str, version: int, **params):
        """One call, no re-login. JSON -> dict; anything else -> {'_binary': True, ...}."""
        path = self.apis.get(api, {}).get("path", "entry.cgi")
        data = {"api": api, "method": method, "version": str(version), **{k: str(v) for k, v in params.items()}}
        headers = {}
        if self.sid:
            data["_sid"] = self.sid
        if self.syno_token:
            headers["X-SYNO-TOKEN"] = self.syno_token
        r = self.http.post(f"{self.base}/webapi/{path}", data=data, headers=headers)
        ctype = r.headers.get("content-type", "")
        if "json" in ctype or r.text[:1] == "{":
            return r.json()
        return {"_binary": True, "status": r.status_code, "content_type": ctype, "bytes": len(r.content),
                "_content": r.content, "disposition": r.headers.get("content-disposition", "")}

    def discover(self) -> dict:
        res = self.raw("SYNO.API.Info", "query", 1, query="all")
        self.apis = res.get("data", {}) if res.get("success") else {}
        return self.apis

    def login(self) -> None:
        with self._lock:
            if not self.apis:
                self.discover()
            version = min(int(self.apis.get("SYNO.API.Auth", {}).get("maxVersion", 6)), 7)
            self.sid = self.syno_token = ""
            res = self.raw("SYNO.API.Auth", "login", version, account=self.user, passwd=self._password,
                           session="Chat", format="sid", enable_syno_token="yes")
            if not res.get("success"):
                code = res.get("error", {}).get("code")
                raise ChatError(f"Chat sign-in failed: {AUTH_ERRORS.get(code, f'error {code}')}", code)
            self.sid = res["data"].get("sid", "")
            self.syno_token = res["data"].get("synotoken", "")

    def logout(self) -> None:
        with self._lock:
            if self.sid:
                try:
                    self.raw("SYNO.API.Auth", "logout", 6, session="Chat")
                except httpx.HTTPError:
                    pass
                self.sid = ""

    def call(self, api: str, method: str, version: int, **params):
        """Call with automatic sign-in and one retry when the session expired."""
        with self._lock:
            if not self.sid:
                self.login()
            res = self.raw(api, method, version, **params)
            if not res.get("_binary") and not res.get("success"):
                code = res.get("error", {}).get("code")
                if code in SESSION_ERRORS:
                    self.login()
                    res = self.raw(api, method, version, **params)
            return res

    def _ok(self, res, what: str):
        if res.get("_binary") or res.get("success"):
            return res
        code = res.get("error", {}).get("code")
        raise ChatError(f"{what} failed (error {code})", code)

    # --- Chat operations -------------------------------------------------------
    def channels(self) -> list[dict]:
        res = self._ok(self.call("SYNO.Chat.Channel", "list", self.CHANNEL_VERSION, limit=500), "channel list")
        data = res.get("data", {})
        return data.get("channels", []) if isinstance(data, dict) else []

    def posts(self, channel_id: int, anchor: int, next_count: int = 0, prev_count: int = 0,
              thread_id: int | None = None) -> list[dict]:
        """Main posts around `anchor`; with `thread_id`, the replies of that thread after `anchor`
        (the anchor must not be the thread's own first post - use an earlier post or a reply)."""
        extra = {"thread_id": thread_id} if thread_id else {}
        res = self._ok(self.call("SYNO.Chat.Post", "list", self.POST_VERSION, channel_id=channel_id,
                                 post_id=anchor, next_count=next_count, prev_count=prev_count, **extra),
                       "post list")
        data = res.get("data", {})
        posts = data.get("posts", []) if isinstance(data, dict) else []
        return [p for p in posts if p.get("channel_id") == channel_id]

    def first_post(self, channel_id: int) -> dict | None:
        res = self._ok(self.call("SYNO.Chat.Post", "list", self.POST_VERSION, channel_id=channel_id), "post list")
        posts = res.get("data", {}).get("posts", [])
        return posts[0] if posts else None

    def latest_post_id(self, channel_id: int, page: int = 200, max_pages: int = 1000) -> int:
        """Newest post id in a channel (0 if empty)."""
        # Fast path: anchor at the highest possible id of this channel.
        top = (channel_id << 32) + 0xFFFFFFFF
        try:
            posts = self.posts(channel_id, top, prev_count=1)
            if posts:
                return max(p["post_id"] for p in posts)
        except ChatError:
            pass
        # Slow path (once per channel): walk forward from the first post.
        first = self.first_post(channel_id)
        if not first:
            return 0
        last = first["post_id"]
        for _ in range(max_pages):
            newer = [p["post_id"] for p in self.posts(channel_id, last, next_count=page) if p["post_id"] > last]
            if not newer:
                break
            last = max(newer)
            if len(newer) < page:
                break
        return last

    def send(self, channel_id: int, text: str, thread_id: int | None = None) -> bool:
        """Post a plain-text message (as a thread reply when possible)."""
        res = {}
        if thread_id:
            res = self.call("SYNO.Chat.Post", "create", self.POST_VERSION, channel_id=channel_id,
                            message=text, thread_id=thread_id)
        if not res.get("success"):
            res = self.call("SYNO.Chat.Post", "create", self.POST_VERSION, channel_id=channel_id, message=text)
        if not res.get("success"):
            log.warning("Chat post failed: %s", res.get("error"))
            return False
        creator = (res.get("data") or {}).get("creator_id")
        if creator:
            self.me = int(creator)
        return True

    def users(self) -> dict[int, str]:
        """Chat user id -> user name (best effort; empty if the call is not available)."""
        top = int(self.apis.get("SYNO.Chat.User", {}).get("maxVersion", 3))
        for v in range(top, 0, -1):
            try:
                res = self.call("SYNO.Chat.User", "list", v)
            except httpx.HTTPError:
                return {}
            if res.get("success"):
                data = res.get("data", {})
                rows = data.get("users", []) if isinstance(data, dict) else data
                return {int(u["user_id"]): str(u.get("username") or u.get("nickname") or u["user_id"])
                        for u in rows if isinstance(u, dict) and "user_id" in u}
        return {}

    def download(self, post_id: int) -> bytes:
        res = self.call("SYNO.Chat.Post.File", "get", self.FILE_VERSION, post_id=post_id)
        if res.get("_binary") and res.get("status") == 200:
            return res["_content"]
        code = res.get("error", {}).get("code") if not res.get("_binary") else res.get("status")
        raise ChatError(f"file download failed ({code})", code)
