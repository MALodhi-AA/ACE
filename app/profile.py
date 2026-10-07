"""ACE's job description (config/employee.yaml), loaded once."""
from __future__ import annotations

import yaml

from app.config import ROOT_DIR

PROFILE: dict = yaml.safe_load((ROOT_DIR / "config" / "employee.yaml").read_text(encoding="utf-8"))
# How ACE refers to the head of the firm in every message.
MANAGER: str = (PROFILE.get("manager") or {}).get("name") or "the manager"
