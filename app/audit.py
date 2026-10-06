"""Append-only audit log (JSON Lines).

Every request the employee receives and every action it takes is written to
`data/logs/audit.jsonl`. This is the employee's "work diary" and is what you
review when checking what it did and for whom.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.config import settings

_lock = threading.Lock()


def audit(event: str, **fields: Any) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    line = json.dumps(record, default=str, ensure_ascii=False)
    with _lock:
        with open(settings.log_dir / "audit.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def recent(limit: int = 20) -> list[dict]:
    path = settings.log_dir / "audit.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out
