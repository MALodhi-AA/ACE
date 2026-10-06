import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def sent(monkeypatch):
    """Capture everything the employee posts back to Synology Chat."""
    calls = []

    class Resp:
        status_code = 200
        text = '{"success":true}'

    def fake_post(url, data=None, timeout=None, verify=None):
        calls.append({"url": url, "payload": json.loads(data["payload"])})
        return Resp()

    import integrations.synology_chat.client as c
    monkeypatch.setattr(c.httpx, "post", fake_post)
    monkeypatch.setattr(c, "MIN_SEND_INTERVAL", 0)
    return calls


@pytest.fixture()
def client(data_dir):
    from app.main import app
    return TestClient(app)


def bot(client, text, user_id="5", username="ma", token="bot-secret"):
    return client.post("/synology/bot", data={"token": token, "user_id": user_id, "username": username,
                                              "post_id": "1", "timestamp": "0", "text": text})


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_bad_token_rejected(client, sent):
    assert bot(client, "status", token="wrong").status_code == 401
    assert sent == []


def test_status_via_chatbot(client, sent):
    assert bot(client, "status").status_code == 200
    texts = [c["payload"]["text"] for c in sent]
    final = texts[-1]
    assert "ACE" in final and "Status: Online" in final
    assert "Financial Reporting Analyst" in final
    assert "Monthly MIS" in final
    assert "Tally ✗" in final and "AI Model ✗" in final   # no API key in tests
    url = urlparse(sent[-1]["url"])
    q = parse_qs(url.query)
    assert q["method"] == ["chatbot"] and q["token"] == ['"bot-secret"']
    assert sent[-1]["payload"]["user_ids"] == [5]


def test_unauthorised_user(client, sent):
    bot(client, "status", user_id="99", username="intruder")
    assert "not authorised" in sent[-1]["payload"]["text"]


def test_mis_via_chatbot(client, sent):
    bot(client, "mis sample_restaurant")
    texts = [c["payload"]["text"] for c in sent]
    assert texts[0].startswith("On it")
    assert "MIS completed" in "\n".join(texts)


def test_ask_without_key(client, sent):
    bot(client, "Explain EBITDA")
    assert "AI model is not connected" in sent[-1]["payload"]["text"]


def test_slash_command(client, sent):
    r = client.post("/synology/slash", data={"token": "slash-secret", "user_id": "5", "username": "ma",
                                             "channel_id": "1", "channel_name": "finance", "text": "/ace help"})
    assert r.status_code == 200 and "Commands" in r.json()["text"]
    r = client.post("/synology/slash", data={"token": "slash-secret", "user_id": "5", "username": "ma", "text": "/ai status"})
    assert "Checking" in r.json()["text"]
    assert "method=incoming" in sent[-1]["url"] and "Status: Online" in sent[-1]["payload"]["text"]


def test_chunking():
    from integrations.synology_chat.client import chunk_text
    parts = chunk_text("\n".join(f"line {i} " + "x" * 50 for i in range(200)), 1000)
    assert all(len(p) <= 1000 for p in parts) and len(parts) > 5


def test_ask_and_mis_with_fake_model(client, sent, monkeypatch):
    """Exercise the AI path with a stand-in for the Anthropic client."""
    from types import SimpleNamespace
    from app.config import settings
    from app.llm import llm

    prompts = []

    class FakeMessages:
        def create(self, **kw):
            prompts.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(text="EBITDA is earnings before interest, tax, depreciation and amortisation.")],
                                   model=kw["model"], usage=SimpleNamespace(input_tokens=10, output_tokens=12))

    monkeypatch.setattr(settings, "anthropic_api_key", "test")
    monkeypatch.setattr(llm, "_client", SimpleNamespace(messages=FakeMessages()))
    bot(client, "ask Explain EBITDA")
    assert "earnings before interest" in sent[-1]["payload"]["text"]
    assert prompts[-1]["messages"][-1]["content"] == "Explain EBITDA"
    assert "ACE" in prompts[-1]["system"]

    bot(client, "mis sample")
    assert "Monthly MIS Skill" in prompts[-1]["system"] and "DATA:" in prompts[-1]["messages"][0]["content"]
    assert "MIS completed" in sent[-1]["payload"]["text"]


def test_name_prefixes():
    from app.employee import parse
    assert parse("@ACE status") == ("status", "")
    assert parse("ACE, ask Explain EBITDA") == ("ask", "Explain EBITDA")
    assert parse("/ai mis sample") == ("mis", "sample")
    assert parse("Acer laptop advice?")[0] == "acer"       # words starting with ACE are not eaten
    assert parse("/ace status") == ("status", "")
