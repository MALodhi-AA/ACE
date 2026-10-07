"""AI model layer.

All calls to the model go through `LLM.complete()`. Keeping this behind one small
interface means the provider or model can be changed in `.env` without touching
skills or chat code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings

log = logging.getLogger(__name__)


class LLMNotConfigured(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLM:
    def __init__(self) -> None:
        self._client = None

    @property
    def configured(self) -> bool:
        return settings.ai_configured

    def _get_client(self):
        if not self.configured:
            raise LLMNotConfigured("ANTHROPIC_API_KEY is not set in .env")
        if self._client is None:
            import anthropic  # imported lazily so tests run without the key

            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return self._client

    def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int | None = None,
    ) -> LLMResult:
        client = self._get_client()
        resp = client.messages.create(
            model=settings.claude_model,
            max_tokens=max_tokens or settings.claude_max_tokens,
            system=system,
            messages=messages,
        )
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        usage = getattr(resp, "usage", None)
        _usage.add(getattr(resp, "model", settings.claude_model), usage)
        return LLMResult(
            text=text,
            model=getattr(resp, "model", settings.claude_model),
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        )

    def create(self, system: str, messages: list[dict], tools: list[dict] | None = None,
               model: str | None = None, max_tokens: int | None = None):
        """Raw call (used for tool use). Records token usage for the cost line in `status`."""
        client = self._get_client()
        kw = {"tools": tools} if tools else {}
        resp = client.messages.create(model=model or settings.claude_model,
                                      max_tokens=max_tokens or settings.claude_max_tokens,
                                      system=system, messages=messages, **kw)
        usage.add(getattr(resp, "model", model or settings.claude_model), getattr(resp, "usage", None))
        return resp

    def ping(self) -> tuple[bool, str]:
        """Cheap connectivity check used by `status`."""
        if not self.configured:
            return False, "API key missing"
        try:
            r = self.complete("Reply with the single word OK.", [{"role": "user", "content": "ping"}], max_tokens=5)
            return True, r.model
        except Exception as exc:  # noqa: BLE001 - surface any provider error in status
            log.warning("LLM ping failed: %s", exc)
            return False, type(exc).__name__


# --- usage / cost tracking (for `status`) --------------------------------------------
# USD per million tokens (input, output). Override with AI_PRICES="model=in/out;model=in/out".
PRICES = {"haiku": (1.0, 5.0), "sonnet": (2.0, 10.0), "opus": (4.0, 20.0), "fable": (10.0, 50.0)}


class Usage:
    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self.day = None
        self.reset()

    def reset(self) -> None:
        self.calls = {"fast": 0, "full": 0}
        self.cost = 0.0
        self.routes = {"command": 0, "learned": 0, "fast": 0, "full": 0}

    def _today(self) -> None:
        from datetime import date
        if self.day != date.today():
            self.day = date.today()
            self.reset()

    @staticmethod
    def price(model: str) -> tuple[float, float]:
        import os
        for item in os.getenv("AI_PRICES", "").split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                if k.strip() and k.strip() in model:
                    a, b = v.split("/")
                    return float(a), float(b)
        for key, p in PRICES.items():
            if key in (model or "").lower():
                return p
        return PRICES["sonnet"]

    def add(self, model: str, u) -> None:
        if u is None:
            return
        pin, pout = self.price(model)
        with self._lock:
            self._today()
            self.cost += (getattr(u, "input_tokens", 0) * pin + getattr(u, "output_tokens", 0) * pout) / 1e6
            self.calls["fast" if "haiku" in (model or "") else "full"] += 1

    def route(self, kind: str) -> None:
        with self._lock:
            self._today()
            self.routes[kind] = self.routes.get(kind, 0) + 1

    def line(self) -> str:
        with self._lock:
            self._today()
            r = self.routes
            total = sum(r.values())
            return (f"AI today: {total} questions - {r['command'] + r['learned']} without AI, {r['fast']} fast AI, "
                    f"{r['full']} full AI; ~${self.cost:.2f}")


usage = _usage = Usage()
llm = LLM()
