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


def channel_files_store() -> LocalStore | NasStore:
    """Where files shared in Chat channels are saved: ACE/channel-files/..."""
    if nas.configured:
        return NasStore(settings.ace_share, ["channel-files"])
    return LocalStore(settings.data_dir / "channel-files")


def free_name(store, folder: list[str], name: str) -> str:
    """`name` if unused in `folder`, else name_v2.ext, name_v3.ext ... (never overwrite)."""
    if not store.exists(folder + [name]):
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext, dot = name, "", ""
    n = 2
    while True:
        candidate = f"{stem}_v{n}{dot}{ext}"
        if not store.exists(folder + [candidate]):
            return candidate
        n += 1
