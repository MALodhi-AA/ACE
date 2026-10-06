"""Read-only access to client files.

Sources ACE can read from:

- **NAS shares** (main deployment): every shared folder on the Synology Drive
  NAS that the `ace` account can read - one share per client, e.g. `Food Box`.
- **inbox**: the `inbox` folder inside ACE's own share (or a local folder
  when no NAS is configured).
- **Local folders** from ACE_SOURCES (development / Synology-hosted setups).

A reference looks like:

    Food Box/2026/09 Sep/TB.xlsx     -> share "Food Box", path inside it
    Food Box/2026/09 Sep/tb sep      -> fuzzy file name match in that folder
    Mara_Sep2026_TB.xlsx             -> the inbox

Back-slashes are accepted too. Anything that would escape a source (`..`,
absolute paths, drive letters, other servers) is refused, and only Excel/CSV
files can be read.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from app.config import settings
from app.nas import Entry, NasError, nas

READABLE = {".xlsx", ".xlsm", ".xls", ".csv"}
MAX_LIST = 40
SKIP_PREFIXES = ("~$", ".", "#", "@")


class FileAccessError(ValueError):
    """Message is safe to show to the user."""


# ---------------------------------------------------------------------------
# Source back-ends
# ---------------------------------------------------------------------------

class LocalSource:
    kind = "local"

    def __init__(self, name: str, root: Path) -> None:
        self.name, self.root = name, root

    def describe(self) -> str:
        return str(self.root)

    def available(self) -> bool:
        return self.root.exists()

    def _path(self, parts: list[str]) -> Path:
        p = self.root.joinpath(*parts)
        try:
            p.resolve(strict=False).relative_to(self.root.resolve(strict=False))
        except ValueError as exc:
            raise FileAccessError("That path is outside the folders I'm allowed to read.") from exc
        return p

    def is_dir(self, parts: list[str]) -> bool:
        return self._path(parts).is_dir()

    def is_file(self, parts: list[str]) -> bool:
        return self._path(parts).is_file()

    def list(self, parts: list[str]) -> list[Entry]:
        out = []
        for e in self._path(parts).iterdir():
            st = e.stat()
            out.append(Entry(e.name, e.is_dir(), st.st_size, datetime.fromtimestamp(st.st_mtime)))
        return out

    def read(self, parts: list[str]) -> bytes:
        return self._path(parts).read_bytes()


class SmbSource:
    kind = "nas"

    def __init__(self, name: str, share: str, base: list[str] | None = None) -> None:
        self.name, self.share, self.base = name, share, list(base or [])

    def describe(self) -> str:
        return "\\\\" + settings.nas_host + "\\" + "\\".join([self.share, *self.base])

    def available(self) -> bool:
        return nas.is_dir(self.share, self.base)

    def is_dir(self, parts: list[str]) -> bool:
        return nas.is_dir(self.share, self.base + parts)

    def is_file(self, parts: list[str]) -> bool:
        return nas.is_file(self.share, self.base + parts)

    def list(self, parts: list[str]) -> list[Entry]:
        try:
            return nas.scandir(self.share, self.base + parts)
        except NasError as exc:
            raise FileAccessError(str(exc)) from exc

    def read(self, parts: list[str]) -> bytes:
        try:
            return nas.read_bytes(self.share, self.base + parts)
        except NasError as exc:
            raise FileAccessError(str(exc)) from exc


def get_sources(refresh: bool = False) -> dict[str, LocalSource | SmbSource]:
    """All sources ACE may read, keyed by the name users type."""
    out: dict[str, LocalSource | SmbSource] = {}
    if nas.configured:
        out["inbox"] = SmbSource("inbox", settings.ace_share, ["inbox"])
        try:
            for share in nas.readable_shares(refresh=refresh):
                out.setdefault(share, SmbSource(share, share))
        except NasError:
            pass  # status / files report the NAS problem; local sources still work
    for name, root in settings.sources.items():
        if name == "inbox" and "inbox" in out:
            continue
        out.setdefault(name, LocalSource(name, root))
    return out


# ---------------------------------------------------------------------------
# Reference parsing and resolution
# ---------------------------------------------------------------------------

@dataclass
class Resolved:
    source: LocalSource | SmbSource
    parts: list[str]

    @property
    def name(self) -> str:
        return self.parts[-1] if self.parts else self.source.name

    @property
    def ref(self) -> str:
        return "/".join([self.source.name, *self.parts])

    def read(self) -> bytes:
        return self.source.read(self.parts)


def _split(ref: str, sources: dict) -> tuple[str | None, list[str]]:
    ref = (ref or "").strip().strip('"').strip("'").replace("\\", "/")
    raw = [p.strip() for p in PurePosixPath(ref).parts]
    parts = [p for p in raw if p not in ("", "/", ".")]
    if not parts:
        return None, []
    if ref.startswith("/") or ":" in parts[0] or ".." in parts:
        raise FileAccessError("Use a path inside one of my folders, e.g. `Food Box/2026/09 Sep/TB.xlsx`. "
                              "Absolute paths and `..` are not allowed.")
    lookup = {name.lower(): name for name in sources}
    if parts[0].lower() in lookup:
        return lookup[parts[0].lower()], parts[1:]
    return None, parts


def _source(name: str, sources: dict):
    src = sources[name]
    if not src.available():
        if src.kind == "nas" and nas.last_error:
            raise FileAccessError(f"I can't reach the NAS right now: {nas.last_error}")
        raise FileAccessError(f"I can't reach the '{name}' folder ({src.describe()}) right now. "
                              "Check the NAS / network share and my read permission.")
    return src


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        from zoneinfo import ZoneInfo
        dt = dt.astimezone(ZoneInfo(settings.timezone))
    return f"{dt:%d-%b-%Y %H:%M}"


def _visible(e: Entry) -> bool:
    return not e.name.startswith(SKIP_PREFIXES)


def resolve_file(ref: str) -> Resolved:
    """Resolve a user reference to a readable Excel/CSV file inside a source."""
    sources = get_sources()
    name, parts = _split(ref, sources)
    if not parts:
        raise FileAccessError("Which file? e.g. `mis Food Box/2026/09 Sep/TB.xlsx` - send `files` to browse.")
    src = _source(name or "inbox", sources)

    if src.is_file(parts):
        found = parts
    else:
        folder = parts[:-1]
        if not src.is_dir(folder):
            raise FileAccessError(f"Folder not found: {'/'.join([src.name, *folder])}")
        want = parts[-1].lower()
        files = [e for e in src.list(folder) if not e.is_dir and _visible(e)
                 and Path(e.name).suffix.lower() in READABLE]
        exact = [e for e in files if e.name.lower() == want or Path(e.name).stem.lower() == want]
        partial = [e for e in files if want in e.name.lower()]
        if exact:
            found = folder + [exact[0].name]
        elif len(partial) == 1:
            found = folder + [partial[0].name]
        elif partial:
            raise FileAccessError("More than one file matches: " + ", ".join(e.name for e in partial[:6]))
        else:
            raise FileAccessError(f"No file matching '{parts[-1]}' in {'/'.join([src.name, *folder])}. "
                                  "Send `files <folder>` to see what is there.")
    if Path(found[-1]).suffix.lower() not in READABLE:
        raise FileAccessError(f"I can only read Excel/CSV files ({', '.join(sorted(READABLE))}).")
    return Resolved(src, found)


def list_folder(ref: str = "") -> str:
    """Chat-friendly listing of a folder, or of the sources themselves."""
    sources = get_sources(refresh=not (ref or "").strip())
    name, parts = _split(ref, sources)
    if name is None and not parts:
        lines = ["Folders I can read (read-only):"]
        if nas.configured and nas.last_error:
            lines.append(f"⚠️ NAS problem: {nas.last_error}")
        elif nas.configured and len([s for s in sources.values() if s.kind == "nas"]) <= 1:
            lines.append("- (no client folders yet - give the `ace` account Read only on a client's shared folder in DSM)")
        for src in sources.values():
            ok = src.available()
            label = "my inbox" if src.name == "inbox" else ""
            lines.append(f"- {src.name}  {'✓' if ok else '✗ unreachable'}" + (f"  ({label})" if label else ""))
        lines += ["", "Browse with `files <folder>`, e.g. `files Food Box/2026`."]
        return "\n".join(lines)
    src = _source(name or "inbox", sources)
    if not src.is_dir(parts):
        raise FileAccessError(f"Folder not found: {'/'.join([src.name, *parts])}")

    entries = [e for e in src.list(parts) if _visible(e)]
    dirs = sorted((e for e in entries if e.is_dir), key=lambda e: e.name.lower())
    files = sorted((e for e in entries if not e.is_dir and Path(e.name).suffix.lower() in READABLE),
                   key=lambda e: (e.mtime.timestamp() if e.mtime else 0), reverse=True)
    here = "/".join([src.name, *parts])
    lines = [f"{here}/  ({len(dirs)} folders, {len(files)} Excel/CSV files)"]
    lines += [f"📁 {d.name}/" for d in dirs[:MAX_LIST]]
    for f in files[:MAX_LIST]:
        when = _fmt_time(f.mtime)
        lines.append(f"📄 {f.name}  ({f.size/1024:,.0f} KB{', ' + when if when else ''})")
    if len(dirs) > MAX_LIST or len(files) > MAX_LIST:
        lines.append(f"… list truncated to {MAX_LIST} folders / files")
    if not dirs and not files:
        lines.append("(empty)")
    return "\n".join(lines)
