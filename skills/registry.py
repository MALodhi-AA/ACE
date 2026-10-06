"""Registry of the skills the employee currently has.

To give the employee a new skill: create the folder, then add one line below.
"""
from __future__ import annotations

from pathlib import Path

from skills.base import Skill

SKILLS_DIR = Path(__file__).resolve().parent


def load_skills() -> dict[str, Skill]:
    from skills.monthly_mis.skill import run as monthly_mis_run

    skills = [
        Skill.load(SKILLS_DIR / "monthly_mis", monthly_mis_run),
    ]
    return {s.key: s for s in skills}


SKILLS = load_skills()


def by_command(command: str) -> Skill | None:
    command = command.lower()
    for s in SKILLS.values():
        if s.command == command:
            return s
    return None
