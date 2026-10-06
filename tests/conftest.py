import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Isolated data folder and test credentials - set BEFORE the app is imported.
TEST_DATA = ROOT / ".pytest_data"
os.environ.update({
    "DATA_DIR": str(TEST_DATA),
    "ANTHROPIC_API_KEY": "",
    "SYNOLOGY_BASE_URL": "http://nas.test:5000",
    "SYNOLOGY_BOT_TOKEN": "bot-secret",
    "SYNOLOGY_SLASH_TOKEN": "slash-secret",
    "SYNOLOGY_INCOMING_WEBHOOK_URL": "http://nas.test:5000/webapi/entry.cgi?api=SYNO.Chat.External&method=incoming&version=2&token=%22x%22",
    "ALLOWED_USERS": "5,ma",
    "ACE_SOURCES": f"Clients={TEST_DATA / 'clients'};Missing={TEST_DATA / 'not_mounted'}",
})


@pytest.fixture(scope="session", autouse=True)
def data_dir():
    if TEST_DATA.exists():
        shutil.rmtree(TEST_DATA)
    (TEST_DATA / "inbox").mkdir(parents=True)
    from tools.make_sample_data import make_sample

    sample = make_sample()
    shutil.copy(sample, TEST_DATA / "inbox" / sample.name)
    month = TEST_DATA / "clients" / "Mara" / "2026" / "09 Sep"
    month.mkdir(parents=True)
    shutil.copy(sample, month / "Mara TB Sep 2026.xlsx")
    (month / "notes.txt").write_text("not a spreadsheet")
    (TEST_DATA / "secret.xlsx").write_bytes(b"outside every source")
    yield TEST_DATA
    shutil.rmtree(TEST_DATA, ignore_errors=True)
