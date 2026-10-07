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

## Chat as a user (channels)

- `integrations/synology_chat/user_client.py` – ACE's own Chat account (Chat web API:
  channel list, post list with `post_id` + `next_count`/`prev_count`, post create, file get).
  Post ids are `(channel_id << 32) + n`. Not an official Synology API.
- `integrations/synology_chat/watcher.py` – polls channels, saves shared files via
  `app/storage.channel_files_store()`, keeps progress in `STATE_DIR/chat_watch.json`.
- `python -m integrations.synology_chat.probe` – diagnostics against the real Chat NAS
  (message text and secrets are never printed).
