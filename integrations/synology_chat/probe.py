"""Synology Chat user-account probe (v0.4 feasibility test).

Signs in to the Chat NAS as ACE's own Chat *user* (not the bot) and reports which
Chat web APIs exist and what their replies look like, so ACE can later read the
channels it has been added to and collect shared files.

Privacy: message text, passwords, session ids and tokens are never printed -
they are replaced by "<redacted len=N>". Lists are cut to the first 3 items.

Run on the HP server:
    docker compose exec ace python -m integrations.synology_chat.probe
    docker compose exec ace python -m integrations.synology_chat.probe --channel ace-test
    docker compose exec ace python -m integrations.synology_chat.probe --call SYNO.Chat.Channel list 2 limit=5

Needs CHAT_USER and CHAT_PASSWORD in .env (ACE's Synology Chat account on the DS723+).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from app.config import settings

SECRET_KEYS = {"message", "text", "content", "body", "passwd", "password", "sid", "synotoken",
               "token", "did", "device_id", "preview", "snippet", "last_message", "props"}
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


def redact(obj, depth=0):
    if isinstance(obj, dict):
        return {k: (f"<redacted len={len(str(v))}>" if k.lower() in SECRET_KEYS and v not in (None, "", [], {})
                    else redact(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, list):
        out = [redact(v, depth + 1) for v in obj[:3]]
        if len(obj) > 3:
            out.append(f"... {len(obj) - 3} more")
        return out
    return obj


def show(title, data):
    print(f"\n=== {title} ===")
    print(json.dumps(redact(data), indent=2, ensure_ascii=False)[:6000])


class Chat:
    def __init__(self, base: str, verify: bool):
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=30, verify=verify)
        self.sid = ""
        self.syno_token = ""
        self.apis: dict[str, dict] = {}

    def call(self, api, method, version, **params):
        info = self.apis.get(api, {})
        path = info.get("path", "entry.cgi")
        data = {"api": api, "method": method, "version": str(version), **params}
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

    def discover(self):
        res = self.call("SYNO.API.Info", "query", 1, query="all")
        self.apis = res.get("data", {}) if res.get("success") else {}
        return self.apis

    def login(self, user, password):
        auth = self.apis.get("SYNO.API.Auth", {"maxVersion": 6})
        version = min(int(auth.get("maxVersion", 6)), 7)
        res = self.call("SYNO.API.Auth", "login", version, account=user, passwd=password,
                        session="Chat", format="sid", enable_syno_token="yes")
        if res.get("success"):
            self.sid = res["data"].get("sid", "")
            self.syno_token = res["data"].get("synotoken", "")
        return res

    def logout(self):
        if self.sid:
            try:
                self.call("SYNO.API.Auth", "logout", 6, session="Chat")
            except httpx.HTTPError:
                pass


def find_files(obj, path=""):
    """Return (path, value) for every dict that looks like a file attachment."""
    hits = []
    if isinstance(obj, dict):
        keys = {k.lower() for k in obj}
        if keys & {"file_id", "file_name", "filename", "file", "attachment", "attachments", "file_props"}:
            hits.append((path, obj))
        for k, v in obj.items():
            hits += find_files(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += find_files(v, f"{path}[{i}]")
    return hits


def channel_list(chat, chat_apis):
    for api in [a for a in chat_apis if a.endswith(".Channel")] + [a for a in chat_apis if "Channel" in a]:
        top = int(chat.apis[api].get("maxVersion", 1))
        for v in range(top, 0, -1):
            for params in ({"limit": 100}, {}):
                res = chat.call(api, "list", v, **params)
                if res.get("success"):
                    return api, v, res
                show(f"{api} list v{v} {params}", res)
    return None, None, None


def items(res):
    data = res.get("data", {}) if isinstance(res, dict) else {}
    if isinstance(data, list):
        return data
    for key in ("channels", "posts", "items", "list", "data"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", help="name of a test channel ACE has been added to")
    ap.add_argument("--call", nargs="+", metavar="ARG", help="API METHOD VERSION [key=value ...]")
    args = ap.parse_args()

    user, password = os.getenv("CHAT_USER", ""), os.getenv("CHAT_PASSWORD", "")
    if not settings.synology_base_url or not user or not password:
        print("Set SYNOLOGY_BASE_URL, CHAT_USER and CHAT_PASSWORD in .env, then `docker compose up -d`.")
        return 2

    chat = Chat(settings.synology_base_url, settings.synology_verify_ssl)
    print(f"Chat NAS: {settings.synology_base_url}   user: {user}")
    try:
        chat.discover()
        chat_apis = sorted(a for a in chat.apis if a.startswith("SYNO.Chat"))
        print(f"\nDSM APIs found: {len(chat.apis)}; Synology Chat APIs: {len(chat_apis)}")
        for a in chat_apis:
            i = chat.apis[a]
            print(f"  {a:45} {i.get('path', ''):22} v{i.get('minVersion')}-{i.get('maxVersion')}")
        if not chat_apis:
            print("No SYNO.Chat APIs - is Synology Chat Server installed on this NAS?")
            return 1

        res = chat.login(user, password)
        if not res.get("success"):
            code = res.get("error", {}).get("code")
            print(f"\nLOGIN FAILED (code {code}): {AUTH_ERRORS.get(code, 'see DSM API error codes')}")
            return 1
        print("\nLOGIN OK (session id and token hidden)")

        if args.call:
            api, method, version, *rest = args.call
            params = dict(p.split("=", 1) for p in rest)
            out = chat.call(api, method, int(version), **params)
            if out.get("_binary"):
                out.pop("_content")
            show(f"{api} {method} v{version} {params}", out)
            return 0

        api, v, res = channel_list(chat, chat_apis)
        if not res:
            print("\nCould not list channels with the automatic guesses - send me this output.")
            return 1
        channels = items(res)
        print(f"\nCHANNELS OK via {api} v{v}: {len(channels)} visible to ACE")
        for c in channels[:30]:
            print(f"  id={c.get('channel_id', c.get('id'))}  name={c.get('name', c.get('display_name'))!r}  "
                  f"type={c.get('type')}")
        show("first channel (fields)", channels[0] if channels else {})

        if not args.channel:
            print("\nNext: add ACE to a test channel and run again with --channel <name>.")
            return 0
        target = next((c for c in channels if str(c.get("name", "")).lower() == args.channel.lower()), None)
        if not target:
            print(f"\nChannel {args.channel!r} not found - has ACE been added to it?")
            return 1
        cid = target.get("channel_id", target.get("id"))

        post_apis = [a for a in chat_apis if a.endswith(".Post")] + [a for a in chat_apis if "Post" in a]
        posts_res, papi, pv = None, None, None
        for a in dict.fromkeys(post_apis):
            for ver in range(int(chat.apis[a].get("maxVersion", 1)), 0, -1):
                r = chat.call(a, "list", ver, channel_id=cid, limit=20)
                if r.get("success"):
                    posts_res, papi, pv = r, a, ver
                    break
                show(f"{a} list v{ver}", r)
            if posts_res:
                break
        if not posts_res:
            print("\nCould not read posts with the automatic guesses - send me this output.")
            return 1
        posts = items(posts_res)
        print(f"\nPOSTS OK via {papi} v{pv}: {len(posts)} recent posts read from {args.channel!r}")
        show("first post (fields; text hidden)", posts[0] if posts else {})

        hits = find_files(posts)
        print(f"\nPosts with file attachments: {len(hits)}")
        for p, f in hits[:3]:
            show(f"file at {p}", f)
        if not hits:
            print("Share a small test file in the channel and run again.")
            return 0

        # Try to download the first file and save it under /data/probe/
        post = next((x for x in posts if find_files(x)), {})
        post_id = post.get("post_id", post.get("id"))
        file_apis = [a for a in chat_apis if "File" in a or "Attach" in a or a.endswith(".Post")]
        for a in dict.fromkeys(file_apis):
            for method in ("get", "download"):
                for ver in range(int(chat.apis[a].get("maxVersion", 1)), 0, -1):
                    r = chat.call(a, method, ver, post_id=post_id)
                    if r.get("_binary") and r.get("status") == 200 and r.get("bytes", 0) > 0:
                        out = Path(settings.data_dir) / "probe"
                        out.mkdir(parents=True, exist_ok=True)
                        dest = out / f"post_{post_id}.bin"
                        dest.write_bytes(r["_content"])
                        print(f"\nDOWNLOAD OK via {a} {method} v{ver}: {r['bytes']} bytes, "
                              f"{r['content_type']} -> {dest}")
                        return 0
        print("\nCould not download the file with the automatic guesses - send me this output.")
        return 1
    except httpx.HTTPError as exc:
        print(f"\nCannot reach {settings.synology_base_url}: {exc}")
        return 1
    finally:
        chat.logout()


if __name__ == "__main__":
    sys.exit(main())
