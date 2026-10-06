"""Skill framework.

A *skill* is a documented, version-controlled procedure the employee follows.
Each skill lives in its own folder under `skills/`:

    skills/<skill_key>/
        skill.yaml     name, version, owner, thresholds/settings
        SKILL.md       the written procedure (your professional method)
        CHANGELOG.md   what changed in each version and why
        skill.py       `run()` - the code that executes the procedure

Calculations are done deterministically in Python. The AI model is used only
for judgement and commentary, and is given the computed figures - it is never
asked to do the arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


@dataclass
class SkillResult:
    ok: bool
    chat_summary: str
    report_path: str | None = None   # local path or NAS location (share/reports/...)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    key: str
    name: str
    version: str
    description: str
    command: str
    usage: str
    folder: Path
    config: dict[str, Any]
    run: Callable[..., SkillResult]

    @property
    def procedure(self) -> str:
        p = self.folder / "SKILL.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    @classmethod
    def load(cls, folder: Path, run: Callable[..., SkillResult]) -> "Skill":
        cfg = yaml.safe_load((folder / "skill.yaml").read_text(encoding="utf-8")) or {}
        return cls(
            key=folder.name,
            name=cfg.get("name", folder.name),
            version=str(cfg.get("version", "0.0")),
            description=cfg.get("description", ""),
            command=cfg.get("command", folder.name),
            usage=cfg.get("usage", ""),
            folder=folder,
            config=cfg,
            run=run,
        )
