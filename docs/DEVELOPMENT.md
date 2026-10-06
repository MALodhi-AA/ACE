# Development notes

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest                      # unit tests; SMB integration tests are skipped
```

### SMB integration tests (real Samba server)

`tests/test_nas_smb.py` checks ACE against a real SMB server laid out like the
Synology Drive NAS: one share per client, an `ace` account with read-only access
to some shares and no access to others, and ACE's own read/write share.

On Linux (or WSL / GitHub Actions):

```bash
sudo apt-get install -y samba smbclient
sudo bash tools/smb_test_server.sh
ACE_TEST_SMB=127.0.0.1:4445 ACE_TEST_SMB_PASSWORD=AcePass123 pytest -v
```

GitHub Actions runs both the unit and the SMB tests on every push.

## How file access works

- `app/nas.py` – SMB client for the NAS (share discovery via `smbclient -L`,
  file I/O via `smbprotocol`). Writes are only allowed to `ACE_SHARE`.
- `app/files.py` – what users can browse/read: NAS shares the `ace` account can
  read, the inbox, and optional local folders (`ACE_SOURCES`). Path-escape
  protection and the Excel/CSV-only rule live here.
- `app/storage.py` – where reports are saved (NAS `ACE/reports` or local folder).
