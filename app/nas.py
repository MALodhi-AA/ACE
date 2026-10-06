"""SMB access to the Synology Drive NAS (AA-RS) as the `ace` account.

ACE behaves like a staff PC: it signs in to the NAS with its own account and
can see exactly the shared folders that account has been given in DSM
(Control Panel > Shared Folder > Edit > Permissions). Nothing about client
folders is configured in ACE itself.

- Share discovery uses Samba's `smbclient -L` (installed in the container);
  set NAS_SHARES to a fixed list if shares are hidden from browsing.
- File access uses the pure-Python `smbprotocol` library (SMB 2/3, signing,
  encryption).
- ACE only ever WRITES to its own share (ACE_SHARE); that is enforced here
  and, independently, by the `ace` account's read-only DSM permissions.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from app.config import settings

log = logging.getLogger(__name__)

SHARE_CACHE_SECONDS = 60


class NasError(RuntimeError):
    """Message is safe to show to the user."""


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    mtime: datetime | None


class NasClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registered = False
        self._shares: list[str] = []
        self._shares_at = 0.0
        self._authfile: str | None = None
        self.last_error: str = ""

    # ------------------------------------------------------------------ basics
    @property
    def configured(self) -> bool:
        return settings.nas_configured

    def _unc(self, share: str, parts: list[str] | tuple[str, ...] = ()) -> str:
        path = rf"\\{settings.nas_host}\{share}"
        return path + ("\\" + "\\".join(parts) if parts else "")

    def _kw(self) -> dict:
        return {"port": settings.nas_port}

    def _ensure_session(self) -> None:
        import smbclient  # lazy: tests without SMB don't need the import cost

        with self._lock:
            if self._registered:
                return
            try:
                smbclient.register_session(
                    settings.nas_host, username=settings.nas_user, password=settings.nas_password,
                    port=settings.nas_port, connection_timeout=10,
                )
            except Exception as exc:  # noqa: BLE001
                raise NasError(f"I can't sign in to the NAS ({settings.nas_host}) as '{settings.nas_user}': "
                               f"{type(exc).__name__}. Check NAS_HOST/NAS_USER/NAS_PASSWORD and that SMB is "
                               "allowed for that account.") from exc
            self._registered = True

    def _call(self, fn, *args, **kwargs):
        """Run an smbclient call, re-connecting once if the connection dropped."""
        import smbclient
        from smbprotocol.exceptions import SMBOSError

        self._ensure_session()
        try:
            return fn(*args, **kwargs, **self._kw())
        except SMBOSError:
            raise
        except Exception:  # noqa: BLE001 - stale connection after NAS restart / network blip
            log.info("SMB call failed; resetting connection and retrying once")
            smbclient.reset_connection_cache()
            with self._lock:
                self._registered = False
            self._ensure_session()
            return fn(*args, **kwargs, **self._kw())

    # ------------------------------------------------------------------ shares
    def _discover(self) -> list[str]:
        if settings.nas_shares:
            return list(settings.nas_shares)
        exe = shutil.which("smbclient")
        if not exe:
            raise NasError("Share discovery needs the `smbclient` tool (installed in the Docker image). "
                           "Alternatively list the client shares in NAS_SHARES.")
        if self._authfile is None:
            fd, path = tempfile.mkstemp(prefix="ace-smb-", text=True)
            with os.fdopen(fd, "w") as fh:
                fh.write(f"username = {settings.nas_user}\npassword = {settings.nas_password}\n")
            os.chmod(path, 0o600)
            self._authfile = path
        cmd = [exe, "-L", f"//{settings.nas_host}", "-p", str(settings.nas_port), "-A", self._authfile,
               "-m", "SMB3", "-g"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        except subprocess.TimeoutExpired as exc:
            raise NasError(f"The NAS ({settings.nas_host}) did not answer in time.") from exc
        shares = [ln.split("|")[1] for ln in out.stdout.splitlines()
                  if ln.startswith("Disk|") and len(ln.split("|")) >= 2]
        if not shares and out.returncode != 0:
            msg = (out.stderr or out.stdout).strip().splitlines()[-1:] or ["unknown error"]
            raise NasError(f"I couldn't list the NAS shares: {msg[0]}")
        return shares

    def readable_shares(self, refresh: bool = False) -> list[str]:
        """Client shares the `ace` account can read (cached for a minute)."""
        if not self.configured:
            return []
        if not refresh and self._shares and time.monotonic() - self._shares_at < SHARE_CACHE_SECONDS:
            return list(self._shares)
        import smbclient

        excluded = {s.lower() for s in settings.nas_exclude} | {settings.ace_share.lower()}
        readable = []
        try:
            discovered = self._discover()
        except NasError as exc:
            self.last_error = str(exc)
            raise
        for share in discovered:
            if share.lower() in excluded or share.endswith("$"):
                continue
            try:
                self._call(smbclient.listdir, self._unc(share))
                readable.append(share)
            except Exception:  # noqa: BLE001 - no permission or offline: simply not offered
                continue
        readable.sort(key=str.lower)
        self._shares, self._shares_at = readable, time.monotonic()
        self.last_error = ""
        return list(readable)

    # ------------------------------------------------------------------ files
    def scandir(self, share: str, parts: list[str]) -> list[Entry]:
        import smbclient

        try:
            entries = self._call(lambda path, **kw: list(smbclient.scandir(path, **kw)), self._unc(share, parts))
        except Exception as exc:  # noqa: BLE001
            raise NasError(self._explain(exc, share, parts)) from exc
        out = []
        for e in entries:
            info = e.smb_info
            out.append(Entry(e.name, e.is_dir(), int(info.end_of_file or 0), info.last_write_time))
        return out

    def is_dir(self, share: str, parts: list[str]) -> bool:
        import smbclient

        try:
            return bool(self._call(smbclient.path.isdir, self._unc(share, parts)))
        except Exception:  # noqa: BLE001
            return False

    def is_file(self, share: str, parts: list[str]) -> bool:
        import smbclient

        try:
            return bool(self._call(smbclient.path.isfile, self._unc(share, parts)))
        except Exception:  # noqa: BLE001
            return False

    def read_bytes(self, share: str, parts: list[str], max_mb: int = 100) -> bytes:
        import smbclient

        def _read(path, **kw):
            with smbclient.open_file(path, mode="rb", share_access="rw", **kw) as fh:
                data = fh.read(max_mb * 1024 * 1024 + 1)
            return data

        try:
            data = self._call(_read, self._unc(share, parts))
        except Exception as exc:  # noqa: BLE001
            raise NasError(self._explain(exc, share, parts)) from exc
        if len(data) > max_mb * 1024 * 1024:
            raise NasError(f"That file is larger than {max_mb} MB - too big for me to read.")
        return data

    def write_bytes(self, share: str, parts: list[str], data: bytes) -> None:
        """Write a file - ONLY allowed in ACE's own share."""
        import smbclient

        if share.lower() != settings.ace_share.lower():
            raise NasError("I only write to my own ACE folder.")

        def _write(path, folder, **kw):
            smbclient.makedirs(folder, exist_ok=True, **kw)
            with smbclient.open_file(path, mode="wb", **kw) as fh:
                fh.write(data)

        try:
            self._call(_write, self._unc(share, parts), self._unc(share, parts[:-1]))
        except Exception as exc:  # noqa: BLE001
            raise NasError(self._explain(exc, share, parts, writing=True)) from exc

    def exists(self, share: str, parts: list[str]) -> bool:
        import smbclient

        try:
            return bool(self._call(smbclient.path.exists, self._unc(share, parts)))
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ status
    def status(self) -> tuple[bool, str]:
        if not self.configured:
            return False, "not configured"
        try:
            shares = self.readable_shares(refresh=True)
        except NasError as exc:
            return False, str(exc)[:80]
        ace_ok = self.is_dir(settings.ace_share, [])
        return True, f"{len(shares)} client folders" + ("" if ace_ok else f", '{settings.ace_share}' folder ✗")

    @staticmethod
    def _explain(exc: Exception, share: str, parts: list[str], writing: bool = False) -> str:
        text = str(exc)
        where = "/".join([share, *parts])
        if "ACCESS_DENIED" in text or "0xc0000022" in text:
            return (f"I don't have {'write' if writing else 'read'} permission for '{where}'. "
                    "Ask MA to give the `ace` account access in DSM.")
        if "OBJECT_NAME_NOT_FOUND" in text or "OBJECT_PATH_NOT_FOUND" in text or "BAD_NETWORK_NAME" in text:
            return f"'{where}' was not found on the NAS."
        if isinstance(exc, NasError):
            return str(exc)
        return f"NAS error on '{where}': {type(exc).__name__}"


nas = NasClient()


def as_file(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
