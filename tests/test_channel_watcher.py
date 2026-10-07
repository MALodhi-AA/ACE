"""Channel watcher: ACE saves files shared in its Synology Chat channels."""
import httpx
import pytest

from app.storage import LocalStore, free_name
from integrations.synology_chat.user_client import ChatError, ChatUser
from integrations.synology_chat.watcher import ChannelWatcher, safe_name

CID = 270
BASE = CID << 32


def post(n, kind="normal", name=None, size=3, at=1791336247632):
    p = {"channel_id": CID, "post_id": BASE + n, "type": kind, "create_at": at, "creator_id": 7,
         "delete_at": 0, "message": ""}
    if kind == "file":
        p["file_props"] = {"name": name, "size": size, "type": "pdf"}
    return p


class FakeChat:
    def __init__(self, posts):
        self.all = posts
        self.sent = []
        self.fail_download = 0
        self.last_post_at = 1

    def channels(self):
        return [
            {"channel_id": CID, "name": "ACE-TEST", "type": "private", "is_joined": True,
             "last_post_at": self.last_post_at},
            {"channel_id": 1, "name": "", "type": "public", "is_joined": True, "last_post_at": 0},
            {"channel_id": 267, "name": "synobot", "type": "synobot", "is_joined": True, "last_post_at": 5},
        ]

    def latest_post_id(self, cid):
        ids = [p["post_id"] for p in self.all if p["channel_id"] == cid]
        return max(ids) if ids else 0

    def posts(self, cid, anchor, next_count=0, prev_count=0):
        ps = sorted((p for p in self.all if p["channel_id"] == cid), key=lambda p: p["post_id"])
        after = [p for p in ps if p["post_id"] >= anchor][: next_count + 1]
        return after

    def download(self, pid):
        if self.fail_download:
            self.fail_download -= 1
            raise ChatError("boom")
        return b"abc"

    def send(self, cid, text, thread_id=None):
        self.sent.append((cid, thread_id, text))
        return True

    def new(self, p):
        self.all.append(p)
        self.last_post_at += 1


@pytest.fixture
def setup(tmp_path):
    chat = FakeChat([post(1, "system"), post(2), post(3, "file", "old.pdf")])
    store = LocalStore(tmp_path / "files")
    w = ChannelWatcher(chat, store=store, state_path=tmp_path / "state.json", tz="Asia/Dubai")
    return chat, store, w, tmp_path


def test_first_round_starts_from_newest_post_without_backfill(setup):
    chat, store, w, tmp = setup
    assert w.poll_once() == 0
    assert w.state[str(CID)]["last_id"] == BASE + 3
    assert not (tmp / "files").exists()
    assert w.stats["channels"] == 2          # synobot channel is not watched


def test_new_file_is_saved_and_acknowledged(setup):
    chat, store, w, tmp = setup
    w.poll_once()
    chat.new(post(4))
    chat.new(post(5, "file", "Food Box TB Sep.xlsx"))
    assert w.poll_once() == 1
    saved = tmp / "files" / "ACE-TEST" / "2026-10" / "Food Box TB Sep.xlsx"
    assert saved.read_bytes() == b"abc"
    assert chat.sent[-1][1] == BASE + 5      # reply in the file's thread
    assert "Saved: Food Box TB Sep.xlsx" in chat.sent[-1][2]
    assert w.state[str(CID)]["last_id"] == BASE + 5
    assert w.poll_once() == 0                # nothing handled twice


def test_same_name_is_versioned_never_overwritten(setup):
    chat, store, w, tmp = setup
    w.poll_once()
    chat.new(post(4, "file", "TB.xlsx"))
    chat.new(post(5, "file", "TB.xlsx"))
    assert w.poll_once() == 2
    folder = tmp / "files" / "ACE-TEST" / "2026-10"
    assert sorted(p.name for p in folder.iterdir()) == ["TB.xlsx", "TB_v2.xlsx"]


def test_state_survives_restart_and_catches_up(setup):
    chat, store, w, tmp = setup
    w.poll_once()
    chat.new(post(4, "file", "while offline.pdf"))
    w2 = ChannelWatcher(chat, store=store, state_path=tmp / "state.json", tz="Asia/Dubai")
    assert w2.poll_once() == 1


def test_failed_download_is_retried_then_reported(setup):
    chat, store, w, tmp = setup
    w.poll_once()
    chat.new(post(4, "file", "a.pdf"))
    chat.fail_download = 1
    assert w.poll_once() == 0 and w.state[str(CID)]["last_id"] == BASE + 3
    chat.last_post_at += 1
    assert w.poll_once() == 1                # second attempt works
    chat.new(post(5, "file", "b.pdf"))
    chat.fail_download = 5
    for _ in range(3):
        chat.last_post_at += 1
        w.poll_once()
    assert w.state[str(CID)]["last_id"] == BASE + 5
    assert "couldn't save b.pdf" in chat.sent[-1][2]


def test_unnamed_channel_and_unsafe_file_names():
    assert safe_name('a/b\\c:d*?.pdf') == "a_b_c_d__.pdf"
    assert safe_name("", "channel-1") == "channel-1"
    w = ChannelWatcher.__new__(ChannelWatcher)
    assert w.folder_name({"channel_id": 1, "name": ""}) == "channel-1"


def test_free_name(tmp_path):
    store = LocalStore(tmp_path)
    assert free_name(store, ["x"], "a.pdf") == "a.pdf"
    store.save(["x", "a.pdf"], b"1")
    store.save(["x", "a_v2.pdf"], b"1")
    assert free_name(store, ["x"], "a.pdf") == "a_v3.pdf"
    store.save(["x", "README"], b"1")
    assert free_name(store, ["x"], "README") == "README_v2"


def _client(handler):
    c = ChatUser("http://nas.test:5000", "ace", "pw")
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    c.apis = {"SYNO.API.Auth": {"path": "entry.cgi", "maxVersion": 7}}
    return c


def test_user_client_relogs_in_and_finds_latest_post():
    calls = {"login": 0}

    def handler(request):
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        if form["api"] == "SYNO.API.Auth":
            calls["login"] += 1
            return httpx.Response(200, json={"success": True, "data": {"sid": f"s{calls['login']}"}})
        if form["api"] == "SYNO.Chat.Post" and form["_sid"] == "s1":
            return httpx.Response(200, json={"success": False, "error": {"code": 119}})
        assert form["post_id"] == str(BASE + 0xFFFFFFFF) and form["prev_count"] == "1"
        return httpx.Response(200, json={"success": True, "data": {"posts": [post(9)]}})

    c = _client(handler)
    assert c.latest_post_id(CID) == BASE + 9
    assert calls["login"] == 2


def test_user_client_reports_login_problem():
    c = _client(lambda r: httpx.Response(200, json={"success": False, "error": {"code": 402}}))
    with pytest.raises(ChatError, match="allow the Synology Chat application"):
        c.login()
