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
        return LLMResult(
            text=text,
            model=getattr(resp, "model", settings.claude_model),
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        )

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


llm = LLM()
