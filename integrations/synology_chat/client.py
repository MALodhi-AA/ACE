"""Synology Chat integration.

Two ways of talking to Chat are supported:

1. **Chatbot** (recommended, new) - Chat > Integration > Bots.
   Chat POSTs each direct message to `/synology/bot`. The employee replies through
   the Chat External API:  method=chatbot, payload={"text": ..., "user_ids": [id]}

2. **Slash command** (your existing setup) - Chat > Integration > Slash Commands.
   Chat POSTs `/ace ...` to `/synology/slash`. Short answers go back in the HTTP
   response; long-running work is posted to a channel via an Incoming Webhook.

Inbound tokens are verified with a constant-time comparison and rejected if missing.
"""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.config import settings

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1800      # keep each chat post comfortably readable
MIN_SEND_INTERVAL = 0.5       # seconds between posts (Chat rate-limits bursts)
_send_lock = threading.Lock()
_last_send = 0.0


@dataclass
class InboundMessage:
    channel: str            # "bot" or "slash"
    user_id: str
    username: str
    text: str
    channel_id: str = ""
    channel_name: str = ""


def verify_token(received: str | None, expected: str) -> bool:
    if not expected or not received:
        return False
    return hmac.compare_digest(received.strip(), expected.strip())


def parse_inbound(form: dict, channel: str) -> InboundMessage:
    return InboundMessage(
        channel=channel,
        user_id=str(form.get("user_id", "")).strip(),
        username=str(form.get("username", "")).strip(),
        text=str(form.get("text", "")).strip(),
        channel_id=str(form.get("channel_id", "")).strip(),
        channel_name=str(form.get("channel_name", "")).strip(),
    )


def chunk_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split on line boundaries so tables/lists are not cut mid-line."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # pathological long line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return [c.rstrip("\n") for c in chunks if c.strip()]


def _chatbot_url() -> str:
    token = quote(f'"{settings.synology_bot_token}"', safe="")
    return f"{settings.synology_base_url}/webapi/entry.cgi?api=SYNO.Chat.External&method=chatbot&version=2&token={token}"


def _post(url: str, payload: dict) -> bool:
    global _last_send
    body = {"payload": json.dumps(payload, ensure_ascii=False)}
    for attempt in range(3):
        with _send_lock:
            wait = MIN_SEND_INTERVAL - (time.monotonic() - _last_send)
            if wait > 0:
                time.sleep(wait)
            try:
                r = httpx.post(url, data=body, timeout=30, verify=settings.synology_verify_ssl)
                _last_send = time.monotonic()
                ok = r.status_code == 200 and '"success":true' in r.text.replace(" ", "")
                if ok:
                    return True
                log.warning("Synology Chat send failed (%s): %s", r.status_code, r.text[:300])
            except httpx.HTTPError as exc:
                _last_send = time.monotonic()
                log.warning("Synology Chat send error: %s", exc)
        time.sleep(0.3 * (2 ** attempt))
    return False


def send_to_user(user_id: str, text: str) -> bool:
    """Send a direct message from the chatbot to one user."""
    if not settings.bot_configured:
        log.error("Chatbot not configured (SYNOLOGY_BASE_URL / SYNOLOGY_BOT_TOKEN)")
        return False
    ok = True
    for part in chunk_text(text):
        ok &= _post(_chatbot_url(), {"text": part, "user_ids": [int(user_id)]})
    return ok


def send_to_channel(text: str) -> bool:
    """Post to the channel behind the Incoming Webhook (used by slash-command mode)."""
    if not settings.synology_incoming_webhook_url:
        log.error("SYNOLOGY_INCOMING_WEBHOOK_URL not configured")
        return False
    ok = True
    for part in chunk_text(text):
        ok &= _post(settings.synology_incoming_webhook_url, {"text": part})
    return ok


def reply(msg: InboundMessage, text: str) -> bool:
    """Deliver an asynchronous reply on whichever channel the request came from."""
    if msg.channel == "bot":
        return send_to_user(msg.user_id, text)
    prefix = f"@{msg.username} " if msg.username else ""
    return send_to_channel(prefix + text)
