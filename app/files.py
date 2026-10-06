"""Read-only access to client files.

ACE may only read files inside the named sources configured in ACE_SOURCES
(plus its own inbox). A reference looks like:

    Clients/Mara/2026/09 Sep/TB.xlsx      -> source "Clients", relative path
    Mara_Sep2026_TB.xlsx                  -> the inbox
    sample                                -> fuzzy match in the inbox

Back-slashes are accepted too (Clients\\Mara\\TB.xlsx). Anything that would
escape a source root (.., absolute paths, other drives or shares) is refused.
Write protection is enforced twice: ACE's code never writes to a source, and
the `ace` account only has read permission on those shares.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from app.config import settings

READABLE = {".xlsx", ".xlsm", ".xls", ".csv"}
MAX_LIST = 40


class FileAccessError(ValueError):
    """Message is safe to show to the user."""


@dataclass
class Resolved:
    source: str
    path: Path

    @property
    def ref(self) -> str:
        root = settings.sources[self.source]
        try:
            rel = self.path.relative_to(root)
            return f"{self.source}/{rel.as_posix()}" if str(rel) != "." else self.source
        except ValueError:
            return self.path.name


def _split(ref: str) -> tuple[str | None, list[str]]:
    ref = (ref or "").strip().strip('"').strip("'").replace("\\", "/")
    parts = [p for p in PurePosixPath(ref).parts if p not in ("", "/", ".")]
    if not parts:
        return None, []
    if ref.startswith("/") or ":" in parts[0] or ".." in parts:
        raise FileAccessError("Use a path inside one of my sources, e.g. `Clients/Mara/TB.xlsx`. "
                              "Absolute paths and `..` are not allowed.")
    lookup = {name.lower(): name for name in settings.sources}
    if parts[0].lower() in lookup:
        return lookup[parts[0].lower()], parts[1:]
    return None, parts


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _root(source: str) -> Path:
    root = settings.sources[source]
    if not root.exists():
        raise FileAccessError(f"I can't reach the '{source}' folder ({root}) right now. "
                              "Check the network share and my read permission.")
    return root


def resolve_file(ref: str) -> Resolved:
    """Resolve a user reference to a readable file inside a source."""
    source, parts = _split(ref)
    if not parts:
        raise FileAccessError("Which file? e.g. `mis Clients/Mara/2026/09/TB.xlsx` - send `files` to browse.")
    source = source or "inbox"
    root = _root(source)
    candidate = root.joinpath(*parts)
    if not _inside(candidate, root):
        raise FileAccessError("That path is outside the folders I'm allowed to read.")

    if candidate.is_file():
        found = candidate
    else:
        # forgiving match on the last part: case-insensitive, extension optional, unique substring
        folder = candidate.parent
        if not folder.is_dir():
            raise FileAccessError(f"Folder not found: {Resolved(source, folder).ref}")
        want = parts[-1].lower()
        files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in READABLE
                 and not f.name.startswith(("~$", "."))]
        exact = [f for f in files if f.name.lower() == want or f.stem.lower() == want]
        partial = [f for f in files if want in f.name.lower()]
        if exact:
            found = exact[0]
        elif len(partial) == 1:
            found = partial[0]
        elif partial:
            raise FileAccessError("More than one file matches: " + ", ".join(f.name for f in partial[:6]))
        else:
            raise FileAccessError(f"No file matching '{parts[-1]}' in {Resolved(source, folder).ref}. "
                                  "Send `files <folder>` to see what is there.")
    if found.suffix.lower() not in READABLE:
        raise FileAccessError(f"I can only read Excel/CSV files ({', '.join(sorted(READABLE))}).")
    if not _inside(found, root):
        raise FileAccessError("That path is outside the folders I'm allowed to read.")
    return Resolved(source, found)


def list_folder(ref: str = "") -> str:
    """Chat-friendly listing of a source folder, or of the sources themselves."""
    source, parts = _split(ref)
    if source is None and not parts:
        lines = ["My file sources (read-only):"]
        for name, root in settings.sources.items():
            ok = root.exists()
            lines.append(f"- {name}  {'✓' if ok else '✗ unreachable'}  ({root})")
        lines += ["", "Browse with `files <source>/<folder>`, e.g. `files Clients/Mara`."]
        return "\n".join(lines)
    if source is None:
        source, parts = "inbox", parts
    root = _root(source)
    folder = root.joinpath(*parts)
    if not _inside(folder, root):
        raise FileAccessError("That path is outside the folders I'm allowed to read.")
    if not folder.is_dir():
        raise FileAccessError(f"Folder not found: {Resolved(source, folder).ref}")

    entries = [e for e in folder.iterdir() if not e.name.startswith(("~$", ".", "#", "@"))]
    dirs = sorted((e for e in entries if e.is_dir()), key=lambda e: e.name.lower())
    files = sorted((e for e in entries if e.is_file() and e.suffix.lower() in READABLE),
                   key=lambda e: e.stat().st_mtime, reverse=True)
    here = Resolved(source, folder).ref
    lines = [f"{here}/  ({len(dirs)} folders, {len(files)} Excel/CSV files)"]
    for d in dirs[:MAX_LIST]:
        lines.append(f"📁 {d.name}/")
    for f in files[:MAX_LIST]:
        st = f.stat()
        lines.append(f"📄 {f.name}  ({st.st_size/1024:,.0f} KB, {datetime.fromtimestamp(st.st_mtime):%d-%b-%Y %H:%M})")
    if len(dirs) > MAX_LIST or len(files) > MAX_LIST:
        lines.append(f"… list truncated to {MAX_LIST} folders / files")
    if not dirs and not files:
        lines.append("(empty)")
    return "\n".join(lines)
