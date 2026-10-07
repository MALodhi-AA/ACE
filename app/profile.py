"""ACE's job description (config/employee.yaml), loaded once."""
from __future__ import annotations

import yaml

from app.config import ROOT_DIR

PROFILE: dict = yaml.safe_load((ROOT_DIR / "config" / "employee.yaml").read_text(encoding="utf-8"))
# How ACE refers to the head of the firm in every message.
MANAGER: str = (PROFILE.get("manager") or {}).get("name") or "the manager"

# name (lower case) -> how ACE writes it, e.g. "amir hussain" -> "Sir Amir Hussain"
TITLES: dict[str, str] = {str(k).strip().lower(): str(v).strip()
                          for k, v in (PROFILE.get("titles") or {}).items()}


def display_name(name: str) -> str:
    """The way ACE must write a person's name ("Amir Hussain" -> "Sir Amir Hussain")."""
    key = (name or "").strip().lower()
    return TITLES.get(key, name)


def persona() -> str:
    """The AI persona plus the list of people's titles."""
    text = PROFILE.get("persona", "")
    if TITLES:
        seen = sorted({v for v in TITLES.values()})
        text += ("\nTitles - always write these people exactly like this: " + ", ".join(seen) + ".\n")
    return text
