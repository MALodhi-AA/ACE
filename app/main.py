"""ACE - web service.

Endpoints
  GET  /health           container health check
  POST /synology/bot     Synology Chat chatbot outgoing URL   (direct messages)
  POST /synology/slash   Synology Chat slash command URL       (/ace in channels)
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.audit import audit
from app.config import settings
from app.employee import PROFILE, Response, handle
from integrations.synology_chat import client as chat

settings.ensure_dirs()

_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _handlers.append(RotatingFileHandler(settings.log_dir / "ace.log", maxBytes=5_000_000, backupCount=5,
                                         encoding="utf-8"))
except OSError:
    pass  # log folder not writable: console/Docker logs only
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), handlers=_handlers,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ace")
# The channel watcher polls Chat every few seconds - keep request lines out of the log.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
app = FastAPI(title=PROFILE["name"], version=PROFILE["version"])


@app.on_event("startup")
def _start_channel_watcher() -> None:
    from integrations.synology_chat import watcher
    watcher.start()


def _run_background(msg: chat.InboundMessage, job) -> None:
    try:
        text = job()
    except Exception as exc:  # noqa: BLE001
        log.exception("background job failed")
        audit("error", where="background", error=repr(exc), user_id=msg.user_id, channel=msg.channel)
        text = f"⚠️ Something went wrong ({type(exc).__name__}). The error has been logged."
    if text:
        if not chat.reply(msg, text):
            audit("error", where="reply", error="delivery failed", user_id=msg.user_id, channel=msg.channel)


async def _form(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        return await request.json()
    return dict(await request.form())


@app.get("/health")
def health():
    return {"status": "ok", "employee": PROFILE["name"], "version": PROFILE["version"]}


@app.post("/synology/bot")
async def synology_bot(request: Request, background: BackgroundTasks):
    form = await _form(request)
    if not chat.verify_token(form.get("token"), settings.synology_bot_token):
        audit("rejected", channel="bot", reason="bad token", user_id=form.get("user_id"))
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    msg = chat.parse_inbound(form, "bot")
    if not msg.user_id:
        return PlainTextResponse("")
    resp: Response = handle(msg.user_id, msg.username, msg.text, "bot")
    # Chatbot replies always go through the Chat API, so slow work never times out.
    if resp.text:
        background.add_task(chat.send_to_user, msg.user_id, resp.text)
    if resp.background:
        background.add_task(_run_background, msg, resp.background)
    return PlainTextResponse("")


@app.post("/synology/slash")
async def synology_slash(request: Request, background: BackgroundTasks):
    form = await _form(request)
    if not chat.verify_token(form.get("token"), settings.synology_slash_token):
        audit("rejected", channel="slash", reason="bad token", user_id=form.get("user_id"))
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    msg = chat.parse_inbound(form, "slash")
    resp: Response = handle(msg.user_id, msg.username, msg.text, "slash")
    if resp.background:
        background.add_task(_run_background, msg, resp.background)
    # Slash commands: the immediate text is returned in the HTTP response.
    return JSONResponse({"text": chat.plain(resp.text or "Thinking…")})
