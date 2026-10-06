"""Integration tests against a real SMB server (Samba), mimicking AA-RS.

Skipped unless ACE_TEST_SMB is set, e.g.

    ACE_TEST_SMB="127.0.0.1:4445" ACE_TEST_SMB_USER=ace ACE_TEST_SMB_PASSWORD=... \
    ACE_TEST_SMB_ROOT=/srv/smbtest pytest tests/test_nas_smb.py

Expected server layout (see docs/DEVELOPMENT.md):
  "Food Box"     - ace: read only   (2026/09 Sep/Food Box TB Sep 2026.xlsx)
  "Adore Beauty" - ace: no access
  "Azhar Backup" - ace: no access
  "ACE"          - ace: read/write  (inbox/, reports/)
"""
import os
import shutil
from pathlib import Path

import pytest

SMB = os.getenv("ACE_TEST_SMB")
pytestmark = pytest.mark.skipif(not SMB, reason="ACE_TEST_SMB not set (no test SMB server)")


@pytest.fixture()
def smb(monkeypatch):
    from app.config import settings
    from app.nas import nas

    host, port = SMB.split(":")
    monkeypatch.setattr(settings, "nas_host", host)
    monkeypatch.setattr(settings, "nas_port", int(port))
    monkeypatch.setattr(settings, "nas_user", os.getenv("ACE_TEST_SMB_USER", "ace"))
    monkeypatch.setattr(settings, "nas_password", os.environ["ACE_TEST_SMB_PASSWORD"])
    monkeypatch.setattr(settings, "ace_share", "ACE")
    monkeypatch.setattr(settings, "nas_shares", [])
    monkeypatch.setattr(settings, "nas_exclude", [])
    monkeypatch.setattr(settings, "sources_raw", "")
    nas._shares, nas._shares_at = [], 0.0
    root = Path(os.getenv("ACE_TEST_SMB_ROOT", "/srv/smbtest"))
    shutil.rmtree(root / "ace" / "reports", ignore_errors=True)
    (root / "ace" / "reports").mkdir(parents=True, exist_ok=True)
    os.chmod(root / "ace" / "reports", 0o777)   # test server runs as uid 'ace'; keep it writable
    yield root
    nas._shares, nas._shares_at = [], 0.0


def test_only_permitted_shares_are_visible(smb):
    from app.nas import nas
    assert nas.readable_shares(refresh=True) == ["Food Box"]   # no access to Adore/Azhar; ACE is its desk


def test_files_listing_over_smb(smb):
    from app.files import list_folder
    top = list_folder("")
    assert "Food Box  ✓" in top and "inbox  ✓" in top and "Adore Beauty" not in top
    month = list_folder("food box/2026/09 Sep")
    assert "Food Box TB Sep 2026.xlsx" in month


def test_mis_reads_from_client_share_and_saves_to_ace_share(smb):
    from skills.monthly_mis.skill import run
    r1 = run("Food Box/2026/09 Sep/tb sep", requested_by="test", use_ai=False)
    r2 = run("Food Box/2026/09 Sep/Food Box TB Sep 2026.xlsx", requested_by="test", use_ai=False)
    assert r1.ok and r2.ok, (r1.chat_summary, r2.chat_summary)
    assert r1.report_path.startswith("ACE/reports/") and r1.report_path.endswith("_v1.xlsx")
    assert r2.report_path.endswith("_v2.xlsx")
    saved = list((smb / "ace" / "reports").rglob("*.xlsx"))
    assert len(saved) == 2
    assert r1.details["source"] == "Food Box/2026/09 Sep/Food Box TB Sep 2026.xlsx"


def test_inbox_on_ace_share(smb):
    from skills.monthly_mis.skill import run
    src = next((smb / "foodbox").rglob("*.xlsx"))
    shutil.copy(src, smb / "ace" / "inbox" / "Dropped TB.xlsx")
    try:
        r = run("dropped", use_ai=False)
        assert r.ok, r.chat_summary
        assert r.details["source"] == "inbox/Dropped TB.xlsx"
    finally:
        (smb / "ace" / "inbox" / "Dropped TB.xlsx").unlink()


def test_no_access_and_no_writes_outside_ace(smb):
    from app.files import FileAccessError, resolve_file
    from app.nas import NasError, nas
    with pytest.raises(FileAccessError):
        resolve_file("Adore Beauty/secret.txt")
    with pytest.raises(NasError, match="only write to my own ACE folder"):
        nas.write_bytes("Food Box", ["x.xlsx"], b"x")
    # even bypassing the code guard, the NAS itself refuses (read-only permission)
    import smbclient
    with pytest.raises(Exception):
        with smbclient.open_file(rf"\\{smb_host()}\Food Box\x.txt", mode="wb", port=smb_port()) as fh:
            fh.write(b"x")


def test_status_reports_nas(smb):
    from app.employee import status_text
    text = status_text(check_ai=False)
    assert "Drive NAS" in text and "✓ - 1 client folders" in text
    assert "Reports folder ✓ (ACE/reports)" in text


def test_fixed_share_list(smb, monkeypatch):
    from app.config import settings
    from app.nas import nas
    monkeypatch.setattr(settings, "nas_shares", ["Food Box", "Adore Beauty"])
    assert nas.readable_shares(refresh=True) == ["Food Box"]


def smb_host():
    return SMB.split(":")[0]


def smb_port():
    return int(SMB.split(":")[1])
