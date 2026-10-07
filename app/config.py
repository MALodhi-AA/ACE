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
    # DATA_DIR holds inbox/, reports/ and logs/ unless each is overridden.
    # On Windows, paths can be local (C:\ACE\logs) or UNC shares (\\RS1619XS\ACE\reports).
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(ROOT_DIR / "data"))))
    inbox_override: str = field(default_factory=lambda: os.getenv("INBOX_DIR", ""))
    reports_override: str = field(default_factory=lambda: os.getenv("REPORTS_DIR", ""))
    log_override: str = field(default_factory=lambda: os.getenv("LOG_DIR", ""))

    # Named READ-ONLY sources ACE may read client files from, e.g.
    #   ACE_SOURCES=Clients=\\RS1619XS\Clients;Accounts=\\RS1619XS\Accounts
    # Separator between sources is ";" (never used in Windows paths).
    sources_raw: str = field(default_factory=lambda: os.getenv("ACE_SOURCES", ""))

    # --- Synology Drive NAS over SMB (v0.3) ---------------------------------
    # ACE signs in to the NAS like a staff PC does and sees exactly the shared
    # folders the `ace` account has permission for. Leave NAS_HOST empty to use
    # local folders only (development / tests / Synology-hosted deployment).
    nas_host: str = field(default_factory=lambda: os.getenv("NAS_HOST", os.getenv("NAS_IP", "")).strip())
    nas_port: int = field(default_factory=lambda: int(os.getenv("NAS_PORT", "445") or 445))
    nas_user: str = field(default_factory=lambda: os.getenv("NAS_USER", ""))
    nas_password: str = field(default_factory=lambda: os.getenv("NAS_PASSWORD", ""))
    # ACE's own share (inbox/ and reports/ inside it) - the ONLY share ACE writes to.
    ace_share: str = field(default_factory=lambda: os.getenv("ACE_SHARE", "ACE").strip())
    # Optional: fixed list of client shares (use if shares are hidden from browsing).
    # Empty = discover every share the `ace` account can read.
    nas_shares: list[str] = field(default_factory=lambda: [s.strip() for s in os.getenv("NAS_SHARES", "").split(";") if s.strip()])
    # Shares never offered as client sources even if `ace` can read them.
    nas_exclude: list[str] = field(default_factory=lambda: [s.strip() for s in os.getenv("NAS_EXCLUDE_SHARES", "").split(";") if s.strip()])

    # --- ACE as a Synology Chat *user* (v0.4: channels) ---------------------
    # ACE's own Chat account on the DS723+ (Chat application only).
    chat_user: str = field(default_factory=lambda: os.getenv("CHAT_USER", "").strip())
    chat_password: str = field(default_factory=lambda: os.getenv("CHAT_PASSWORD", ""))
    # Save every file shared in the channels ACE is a member of.
    chat_watch: bool = field(default_factory=lambda: _bool("CHAT_WATCH", True))
    chat_poll_seconds: int = field(default_factory=lambda: max(3, int(os.getenv("CHAT_POLL_SECONDS", "5") or 5)))
    # ACE's own Chat user id (optional - learnt automatically from its first post)
    chat_user_id: int | None = field(default_factory=lambda: int(os.getenv("CHAT_USER_ID")) if os.getenv("CHAT_USER_ID", "").strip().isdigit() else None)
    chat_max_file_mb: int = field(default_factory=lambda: int(os.getenv("CHAT_MAX_FILE_MB", "200") or 200))
    # Reply under each saved file ("Saved: ...").
    chat_reply_on_save: bool = field(default_factory=lambda: _bool("CHAT_REPLY_ON_SAVE", True))
    # Folder names for channels without a name, e.g. "1=General;2=Random".
    chat_channel_names_raw: str = field(default_factory=lambda: os.getenv("CHAT_CHANNEL_NAMES", ""))
    # Channel ids or names ACE should ignore (";"-separated).
    chat_ignore_raw: str = field(default_factory=lambda: os.getenv("CHAT_IGNORE_CHANNELS", ""))
    state_override: str = field(default_factory=lambda: os.getenv("STATE_DIR", ""))

    timezone: str = field(default_factory=lambda: os.getenv("TZ", "Asia/Dubai"))

    @property
    def chat_user_configured(self) -> bool:
        return bool(self.synology_base_url and self.chat_user and self.chat_password)

    @property
    def chat_channel_names(self) -> dict[str, str]:
        out = {}
        for item in self.chat_channel_names_raw.split(";"):
            if "=" in item:
                k, v = item.split("=", 1)
                if k.strip() and v.strip():
                    out[k.strip()] = v.strip()
        return out

    @property
    def chat_ignore(self) -> set[str]:
        return {v.strip().lower() for v in self.chat_ignore_raw.split(";") if v.strip()}

    @property
    def state_dir(self) -> Path:
        return Path(self.state_override) if self.state_override else self.data_dir / "state"

    @property
    def nas_configured(self) -> bool:
        return bool(self.nas_host and self.nas_user and self.nas_password)

    @property
    def inbox_dir(self) -> Path:
        return Path(self.inbox_override) if self.inbox_override else self.data_dir / "inbox"

    @property
    def reports_dir(self) -> Path:
        return Path(self.reports_override) if self.reports_override else self.data_dir / "reports"

    @property
    def log_dir(self) -> Path:
        return Path(self.log_override) if self.log_override else self.data_dir / "logs"

    @property
    def sources(self) -> dict[str, Path]:
        """Name -> root folder. `inbox` is always available as a source."""
        out: dict[str, Path] = {}
        for item in self.sources_raw.split(";"):
            if "=" not in item:
                continue
            name, path = item.split("=", 1)
            name, path = name.strip(), path.strip().strip('"')
            if name and path:
                out[name] = Path(path)
        out.setdefault("inbox", self.inbox_dir)
        return out

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
        for d in (self.inbox_dir, self.reports_dir, self.log_dir, self.state_dir):
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                # e.g. a read-only or temporarily unreachable network share;
                # `status` reports it instead of crashing at start-up.
                pass


settings = Settings()
