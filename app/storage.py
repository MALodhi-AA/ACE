"""Where ACE saves its work (reports).

On the HP server deployment reports go to `ACE/reports/...` on the Synology
Drive NAS, so staff open them from Synology Drive like any colleague's file.
Without a NAS configured they go to the local REPORTS_DIR.
"""
from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.nas import NasError, nas


class LocalStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def exists(self, parts: list[str]) -> bool:
        return self.root.joinpath(*parts).exists()

    def save(self, parts: list[str], data: bytes) -> str:
        path = self.root.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)


class NasStore:
    def __init__(self, share: str, base: list[str]) -> None:
        self.share, self.base = share, base

    def exists(self, parts: list[str]) -> bool:
        return nas.exists(self.share, self.base + parts)

    def save(self, parts: list[str], data: bytes) -> str:
        try:
            nas.write_bytes(self.share, self.base + parts, data)
        except NasError as exc:
            raise RuntimeError(f"Could not save the report to the NAS: {exc}") from exc
        return "/".join([self.share, *self.base, *parts])


def report_store() -> LocalStore | NasStore:
    if nas.configured:
        return NasStore(settings.ace_share, ["reports"])
    return LocalStore(settings.reports_dir)
