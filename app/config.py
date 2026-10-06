"""Runtime configuration.

Everything secret or environment-specific comes from environment variables
(loaded from `.env` by Docker Compose or python-dotenv). Nothing secret is
ever committed to Git.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so the app also runs outside Docker (no extra dependency)."""
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


@dataclass
class Settings:
    # --- AI model -----------------------------------------------------------
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
    claude_max_tokens: int = field(default_factory=lambda: int(os.getenv("CLAUDE_MAX_TOKENS", "4000")))

    # --- Synology Chat ------------------------------------------------------
    # Base URL of DSM as seen FROM INSIDE the container, e.g. http://192.168.1.10:5000
    synology_base_url: str = field(default_factory=lambda: os.getenv("SYNOLOGY_BASE_URL", "").rstrip("/"))
    synology_bot_token: str = field(default_factory=lambda: os.getenv("SYNOLOGY_BOT_TOKEN", ""))
    synology_slash_token: str = field(default_factory=lambda: os.getenv("SYNOLOGY_SLASH_TOKEN", ""))
    synology_incoming_webhook_url: str = field(
        default_factory=lambda: os.getenv("SYNOLOGY_INCOMING_WEBHOOK_URL", "")
    )
    synology_verify_ssl: bool = field(default_factory=lambda: _bool("SYNOLOGY_VERIFY_SSL", True))

    # --- Security -----------------------------------------------------------
    # Comma-separated Synology Chat user_ids or usernames allowed to use the employee.
    # Empty = everyone in Chat may use it (status will warn).
    allowed_users: list[str] = field(default_factory=lambda: _list("ALLOWED_USERS"))

    # --- Storage ------------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data"))))

    timezone: str = field(default_factory=lambda: os.getenv("TZ", "Asia/Dubai"))

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def ai_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def bot_configured(self) -> bool:
        return bool(self.synology_base_url and self.synology_bot_token)

    @property
    def slash_configured(self) -> bool:
        return bool(self.synology_slash_token and self.synology_incoming_webhook_url)

    def ensure_dirs(self) -> None:
        for d in (self.inbox_dir, self.reports_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
