import pytest

from app.files import FileAccessError, list_folder, resolve_file


def test_sources_listed(data_dir):
    out = list_folder("")
    assert "Clients  ✓" in out and "inbox  ✓" in out and "Missing  ✗ unreachable" in out


def test_browse_client_folder(data_dir):
    out = list_folder("clients/Mara/2026")          # source name is case-insensitive
    assert "📁 09 Sep/" in out
    out = list_folder(r"Clients\Mara\2026\09 Sep")    # Windows-style separators accepted
    assert "Mara TB Sep 2026.xlsx" in out and "notes.txt" not in out


def test_resolve_exact_and_fuzzy(data_dir):
    r = resolve_file("Clients/Mara/2026/09 Sep/Mara TB Sep 2026.xlsx")
    assert r.source.name == "Clients" and r.name == "Mara TB Sep 2026.xlsx"
    assert r.ref == "Clients/Mara/2026/09 Sep/Mara TB Sep 2026.xlsx"
    assert resolve_file("Clients/Mara/2026/09 Sep/tb sep").name == "Mara TB Sep 2026.xlsx"
    assert resolve_file("sample_restaurant").source.name == "inbox"
    assert len(r.read()) > 1000


@pytest.mark.parametrize("ref", [
    "../secret.xlsx",
    "Clients/../secret.xlsx",
    "Clients/Mara/../../../secret.xlsx",
    "/etc/passwd",
    "C:/Windows/win.ini",
    r"\\otherserver\share\file.xlsx",
])
def test_escape_attempts_refused(data_dir, ref):
    with pytest.raises(FileAccessError):
        resolve_file(ref)


def test_non_spreadsheet_refused(data_dir):
    with pytest.raises(FileAccessError):
        resolve_file("Clients/Mara/2026/09 Sep/notes.txt")


def test_unreachable_source_message(data_dir):
    with pytest.raises(FileAccessError, match="can't reach the 'Missing' folder"):
        resolve_file("Missing/x.xlsx")


def test_titles_for_people():
    from app.profile import display_name, persona
    assert display_name("Amir Hussain") == "Sir Amir Hussain"
    assert display_name("muhammad ali lodhi") == "Sir Muhammad Ali"
    assert display_name("Ali") == "Ali"
    assert "Sir Amir Hussain" in persona()


def test_attendance_db_refuses_writes():
    from integrations.attendance.db import AttendanceDB
    db = AttendanceDB(host="x", user="u", password="p")
    for sql in ("UPDATE t SET a=1", "DELETE FROM t", "insert into t values (1)", "DROP TABLE t", "SET x=1"):
        with pytest.raises(PermissionError):
            db.query(sql)
