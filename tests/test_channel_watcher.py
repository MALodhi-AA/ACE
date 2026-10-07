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
    saved = tmp / "files" / "ACE-TEST" / "2026" / "2026-10" / "2026-10-07" / "Food Box TB Sep.xlsx"
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
    folder = tmp / "files" / "ACE-TEST" / "2026" / "2026-10" / "2026-10-07"
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
    """Chat answers 'post not found' (402) for an anchor past the last message - binary search."""
    calls = {"login": 0, "list": 0}
    last = 9

    def handler(request):
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        if form["api"] == "SYNO.API.Auth":
            calls["login"] += 1
            return httpx.Response(200, json={"success": True, "data": {"sid": f"s{calls['login']}"}})
        if form["_sid"] == "s1":
            return httpx.Response(200, json={"success": False, "error": {"code": 119}})
        calls["list"] += 1
        n = int(form["post_id"]) - BASE
        if n > last:
            return httpx.Response(200, json={"success": False, "error": {"code": 402, "errors": "post not found"}})
        lo, hi = n - int(form["prev_count"]), n + int(form["next_count"])
        return httpx.Response(200, json={"success": True, "data": {"posts": [
            post(i) for i in range(max(1, lo), min(last, hi) + 1)]}})

    c = _client(handler)
    assert c.latest_post_id(CID) == BASE + 9
    assert calls["login"] == 2 and calls["list"] < 45
    assert c.first_post(CID)["post_id"] == BASE + 1


def test_user_client_reports_login_problem():
    c = _client(lambda r: httpx.Response(200, json={"success": False, "error": {"code": 402}}))
    with pytest.raises(ChatError, match="allow the Synology Chat application"):
        c.login()


# --- v0.4.1: ACE answers direct messages and @ACE mentions -------------------------
DM = 300
GROUP = 301


class TalkChat(FakeChat):
    me = None

    def channels(self):
        return super().channels() + [
            {"channel_id": DM, "name": "", "type": "anonymous", "total_member_count": 2, "is_joined": True,
             "last_post_at": self.last_post_at},
            {"channel_id": GROUP, "name": "", "type": "anonymous", "total_member_count": 4, "is_joined": True,
             "last_post_at": self.last_post_at}]

    def send(self, cid, text, thread_id=None):
        self.me = 189
        return super().send(cid, text, thread_id)

    def users(self):
        return {5: "ma"}


def msg(cid, n, text, creator=5, mentions=()):
    return {"channel_id": cid, "post_id": (cid << 32) + n, "type": "normal", "create_at": 1791336247632,
            "creator_id": creator, "delete_at": 0, "message": text, "mentions": list(mentions), "thread_id": 0}


@pytest.fixture
def talk(tmp_path):
    chat = TalkChat([post(1, "system"), msg(DM, 1, "old question")])
    w = ChannelWatcher(chat, store=LocalStore(tmp_path / "files"), state_path=tmp_path / "s.json", tz="Asia/Dubai")
    w.poll_once()                            # baseline: old messages are not answered
    assert chat.sent == []
    return chat, w


def test_direct_message_is_answered(talk):
    chat, w = talk
    chat.new(msg(DM, 2, "whoami"))
    w.poll_once()
    assert chat.sent[-1][0] == DM and "user_id: 5" in chat.sent[-1][2]
    assert w.stats["direct"] == 1 and w.stats["answered_today"] == 1
    assert w.stats["channels"] == 3           # ACE-TEST, channel 1, group conversation


def test_channel_message_needs_a_mention(talk):
    chat, w = talk
    chat.new(msg(CID, 4, "just chatting"))
    w.poll_once()
    assert chat.sent == []
    chat.new(msg(CID, 5, "@u:189 whoami", mentions=[189]))
    w.chat.me = 189
    w.poll_once()
    assert chat.sent[-1][0] == CID and "user_id: 5" in chat.sent[-1][2]


def test_own_posts_are_ignored_and_name_mention_works(talk):
    chat, w = talk
    chat.me = 189
    chat.new(msg(CID, 4, "@ACE whoami", creator=189))
    w.poll_once()
    assert chat.sent == []
    chat.new(msg(CID, 5, "@ACE whoami"))
    w.poll_once()
    assert len(chat.sent) == 1


def test_clean_text_and_kind():
    from integrations.synology_chat.watcher import clean_text, kind
    assert clean_text("@u:189 mis Food Box TB") == "mis Food Box TB"
    assert clean_text("@ACE, status") == "status"
    assert kind({"type": "synobot"}) is None and kind({"type": "private"}) == "channel"
    assert kind({"type": "anonymous", "total_member_count": 2}) == "direct"
    assert kind({"type": "anonymous", "total_member_count": 5}) == "group"


def test_group_conversation_needs_a_mention(talk):
    chat, w = talk
    chat.new(msg(GROUP, 1, "lunch?"))
    w.poll_once()
    assert chat.sent == []
    chat.new(msg(GROUP, 2, "@ACE whoami"))
    w.poll_once()
    assert chat.sent[-1][0] == GROUP


# --- v0.4.3: replies inside threads --------------------------------------------------
class ThreadChat(TalkChat):
    """Mimics Chat: replies are hidden from the main list and listed per thread."""

    def posts(self, cid, anchor, next_count=0, prev_count=0, thread_id=None):
        ps = sorted((p for p in self.all if p["channel_id"] == cid), key=lambda p: p["post_id"])
        if thread_id:
            return [p for p in ps if p.get("thread_id") == thread_id and p["post_id"] != thread_id
                    and p["post_id"] >= anchor][: next_count + 1]
        main = [p for p in ps if p.get("thread_id") in (0, None, p["post_id"])]
        before = [p for p in main if p["post_id"] < anchor][-prev_count:] if prev_count else []
        after = [p for p in main if p["post_id"] >= anchor][: next_count + 1]
        return before + after

    def comment(self, root_n, n, **kw):
        root = next(p for p in self.all if p["post_id"] == BASE + root_n)
        p = (post(n, "file", kw["name"]) if "name" in kw else msg(CID, n, kw.get("text", "hi"),
                                                                    mentions=kw.get("mentions", ())))
        p["thread_id"] = root["post_id"]
        root["thread_id"] = root["post_id"]
        root["comment_count"] = root.get("comment_count", 0) + 1
        root["last_comment_at"] = 1791366959588 + n
        self.all.append(p)          # note: channel last_post_at does NOT change for replies


def test_files_and_mentions_inside_threads(tmp_path):
    chat = ThreadChat([post(1, "system"), post(2), post(3, "file", "root.pdf")])
    chat.comment(3, 4, text="old reply before ACE joined")
    w = ChannelWatcher(chat, store=LocalStore(tmp_path / "files"), state_path=tmp_path / "s.json", tz="Asia/Dubai")
    w.poll_once()                     # baseline (channel)
    w.poll_once()                     # thread scan baseline: old reply ignored
    w.poll_once()
    assert chat.sent == []
    chat.comment(3, 5, name="Cred invoice 11 sep 2026_2.png")
    for p in chat.all:                # new replies are newer than the old one
        if p["post_id"] >= BASE + 5:
            p["create_at"] = 1791370000000
    chat.comment(3, 6, text="@u:189 whoami", mentions=[189])
    chat.all[-1]["create_at"] = 1791370000001
    old = next(p for p in chat.all if p["post_id"] == BASE + 4)
    old["mentions"], old["message"] = [189], "@u:189 whoami"   # old question: must stay unanswered
    chat.me = 189
    for _ in range(2):
        w.poll_once()
    saved = tmp_path / "files" / "ACE-TEST" / "2026" / "2026-10" / "2026-10-07" / "Cred invoice 11 sep 2026_2.png"
    assert saved.exists()
    texts = [(t, txt) for _, t, txt in chat.sent]
    assert any(t == BASE + 3 and txt.startswith("Saved:") for t, txt in texts)    # reply in the thread
    assert any(t == BASE + 3 and "user_id: 5" in txt for t, txt in texts)
    assert sum("user_id: 5" in txt for _, txt in texts) == 1      # old question not answered
    n = len(chat.sent)
    for _ in range(2):
        w.poll_once()
    assert len(chat.sent) == n        # nothing handled twice


def test_new_thread_after_start_is_followed(tmp_path):
    chat = ThreadChat([post(1, "system")])
    w = ChannelWatcher(chat, store=LocalStore(tmp_path / "files"), state_path=tmp_path / "s.json", tz="Asia/Dubai")
    w.poll_once(); w.poll_once(); w.poll_once()
    chat.new(post(2, "file", "a.pdf"))
    w.poll_once(); w.poll_once()
    chat.comment(2, 3, name="b.pdf")
    w.poll_once(); w.poll_once()
    folder = tmp_path / "files" / "ACE-TEST" / "2026" / "2026-10" / "2026-10-07"
    assert sorted(p.name for p in folder.iterdir()) == ["a.pdf", "b.pdf"]


# --- v0.5.0: day folders and collecting old files on instruction ------------------------
from datetime import date  # noqa: E402

from app.config import settings  # noqa: E402
from integrations.synology_chat.watcher import parse_collect  # noqa: E402

DAY = 86_400_000


class HistoryChat(ThreadChat):
    def first_post(self, cid):
        ps = sorted((p for p in self.all if p["channel_id"] == cid and p.get("thread_id") in (0, None, p["post_id"])),
                    key=lambda p: p["post_id"])
        return ps[0] if ps else None


def test_parse_collect():
    today = date(2026, 10, 7)
    assert parse_collect("whoami") is None
    c = parse_collect("collect files from 2026-09-01 to 30/09/2026", today)
    assert (c["from"], c["to"]) == (date(2026, 9, 1), date(2026, 9, 30))
    c = parse_collect('collect files from "Payment-Tracker-MaraGroup" for the month of Oct 2026.', today)
    assert c["quoted"] == "Payment-Tracker-MaraGroup"
    assert (c["from"], c["to"]) == (date(2026, 10, 1), date(2026, 10, 31))
    c = parse_collect("Collect files ACE-TEST from 1st Sep 2026", today)
    assert "ACE-TEST" in c["text"] and c["from"] == date(2026, 9, 1) and c["to"] is None
    assert parse_collect("collect files last month", today)["from"] == date(2026, 9, 1)
    c = parse_collect("save all files on 5 October 2026", today)
    assert c["from"] == c["to"] == date(2026, 10, 5)
    assert parse_collect("collect files until 2026-09-30", today)["to"] == date(2026, 9, 30)
    with pytest.raises(ValueError):
        parse_collect("collect files from 2026-13-45", today)


@pytest.fixture
def history(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ace_admins", ["5"])
    t0 = 1788220800000                                    # 2026-09-01 04:00 Dubai
    chat = HistoryChat([post(1, "system", at=t0 - 10 * DAY),
                        post(2, "file", "august.pdf", at=t0 - 5 * DAY),
                        post(3, "file", "sep1.pdf", at=t0),
                        post(4, "normal", at=t0 + DAY),
                        post(5, "file", "sep10.xlsx", at=t0 + 9 * DAY)])
    chat.comment(4, 6, name="in-thread.png")
    chat.all[-1]["create_at"] = t0 + 2 * DAY
    root = next(p for p in chat.all if p["post_id"] == BASE + 4)
    root["last_comment_at"] = t0 + 2 * DAY
    w = ChannelWatcher(chat, store=LocalStore(tmp_path / "files"), state_path=tmp_path / "s.json", tz="Asia/Dubai")
    w.poll_once(); w.poll_once(); w.poll_once()          # nothing old is saved by itself
    assert not (tmp_path / "files").exists()
    return chat, w, tmp_path / "files" / "ACE-TEST"


def files_under(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_collect_by_date_with_threads_in_day_folders(history):
    chat, w, root = history
    assert w.find_channel("ace test") is w.find_channel("Ace-Test") is not None      # loose match
    r = w.collect(w.find_channel("ace-test"), date(2026, 9, 1), date(2026, 9, 30))
    assert r["saved"] == 3
    assert files_under(root) == ["2026/2026-09/2026-09-01/sep1.pdf",
                                 "2026/2026-09/2026-09-03/in-thread.png",
                                 "2026/2026-09/2026-09-10/sep10.xlsx"]
    r = w.collect(w.find_channel("ACE-TEST"))             # all dates: only august is new
    assert r["saved"] == 1 and r["already"] == 3


def test_collect_command_from_direct_chat_and_permissions(history):
    chat, w, root = history
    chat.new(msg(DM, 2, "collect files ACE-TEST from 2026-09-01 to 2026-09-05"))
    w.poll_once()
    w._pool.shutdown(wait=True)
    assert files_under(root) == ["2026/2026-09/2026-09-01/sep1.pdf", "2026/2026-09/2026-09-03/in-thread.png"]
    assert chat.sent[-1][0] == DM and chat.sent[-1][2].startswith("Done: 2 files collected from ACE-TEST")


def test_collect_needs_admin(history, monkeypatch):
    chat, w, root = history
    monkeypatch.setattr(settings, "ace_admins", ["7"])
    chat.new(msg(CID, 7, "@u:189 collect files", mentions=[189]))
    chat.me = 189
    w.poll_once()
    assert "Only my manager" in chat.sent[-1][2]
    assert not root.exists()


def test_collect_natural_language_with_quoted_channel_name(history):
    chat, w, root = history
    chat.new(msg(DM, 2, 'collect files from "Ace Test" for the month of Sep 2026.'))
    w.poll_once()
    w._pool.shutdown(wait=True)
    assert chat.sent[-1][2].startswith("Done: 3 files collected from ACE-TEST from 01-Sep-2026 to 30-Sep-2026")


def test_collect_unknown_channel_suggests_a_name(history):
    chat, w, root = history
    chat.new(msg(DM, 2, 'collect files from "ACE-TST"'))
    w.poll_once()
    assert "Did you mean ACE-TEST?" in chat.sent[-1][2]


def test_collect_reads_whole_history_when_chat_caps_page_size(tmp_path, monkeypatch):
    """Chat may return fewer posts than asked for; ACE must keep paging, not stop."""
    monkeypatch.setattr(settings, "ace_admins", ["5"])

    class Capped(HistoryChat):
        def posts(self, cid, anchor, next_count=0, prev_count=0, thread_id=None):
            return super().posts(cid, anchor, min(next_count, 5), min(prev_count, 5), thread_id)

    t0 = 1788220800000
    chat = Capped([post(n, "normal", at=t0 + n * 1000) for n in range(1, 40)]
                  + [post(40, "file", "late.pdf", at=t0 + DAY * 30)])
    w = ChannelWatcher(chat, store=LocalStore(tmp_path / "f"), state_path=tmp_path / "s.json", tz="Asia/Dubai")
    w.poll_once()
    assert w.state[str(CID)]["last_id"] == BASE + 40          # baseline really is the newest post
    r = w.collect(w.find_channel("ACE-TEST"))
    assert r["saved"] == 1 and r["checked"] == 40


def test_old_state_is_rebaselined(tmp_path):
    import json
    (tmp_path / "s.json").write_text(json.dumps({str(CID): {"last_id": BASE + 1, "last_at": 0}, "_me": {"id": 189}}))
    chat = FakeChat([post(1, "system"), post(2, "file", "old.pdf"), post(3)])
    w = ChannelWatcher(chat, store=LocalStore(tmp_path / "f"), state_path=tmp_path / "s.json", tz="Asia/Dubai")
    assert w.poll_once() == 0 and w.state[str(CID)]["last_id"] == BASE + 3     # old.pdf not saved
    assert w.state["_me"] == {"id": 189}


def test_send_waits_when_chat_says_too_fast(monkeypatch):
    import integrations.synology_chat.user_client as uc
    monkeypatch.setattr(uc.time, "sleep", lambda s: None)
    tries = {"n": 0}

    def handler(request):
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        if form["api"] == "SYNO.API.Auth":
            return httpx.Response(200, json={"success": True, "data": {"sid": "s"}})
        tries["n"] += 1
        if tries["n"] < 3:
            return httpx.Response(200, json={"success": False, "error": {"code": 411, "errors": "create post too fast"}})
        return httpx.Response(200, json={"success": True, "data": {"creator_id": 189}})

    c = _client(handler)
    assert c.send(CID, "Done") is True and tries["n"] == 3 and c.me == 189


def test_empty_chat_does_not_block_other_chats(tmp_path):
    """An empty chat answers 'post not found'; ACE must carry on with the other chats."""
    EMPTY = 271

    class WithEmpty(TalkChat):
        def channels(self):
            return [{"channel_id": EMPTY, "name": "", "type": "anonymous", "total_member_count": 2,
                     "is_joined": True, "last_post_at": self.last_post_at}] + super().channels()

        def latest_post_id(self, cid):
            return 0 if cid == EMPTY else super().latest_post_id(cid)

        def posts(self, cid, anchor, next_count=0, prev_count=0, thread_id=None):
            if cid == EMPTY and not any(p["channel_id"] == EMPTY for p in self.all):
                raise ChatError("post list failed (error 402)", 402)
            return super().posts(cid, anchor, next_count, prev_count)

    chat = WithEmpty([post(1, "system")])
    w = ChannelWatcher(chat, store=LocalStore(tmp_path / "f"), state_path=tmp_path / "s.json", tz="Asia/Dubai")
    w.poll_once()
    chat.new(post(2, "file", "a.pdf"))
    assert w.poll_once() == 1                                  # ACE-TEST still handled
    chat.new(msg(EMPTY, 1, "whoami"))                          # first message in the empty chat
    w.poll_once()
    assert chat.sent[-1][0] == EMPTY and "user_id: 5" in chat.sent[-1][2]
