# Development notes

## House style for documents (v0.8.1)

Every file ACE writes must use the Accountability Accountants branding. Build Excel output with
`app.brand.Sheet` (navy title bar, grey sub-title, green accent line, teal headers, green-tint bands,
navy-tint subtotals, double-underlined totals, Arial, gridlines off, landscape, firm footer) and the
formats `AED` / `AED0` / `PCT`. Put the firm logo top-left on the first sheet: `Sheet(..., first=True)`
does it automatically when `ACE/branding/logo.png` exists on the NAS. Never redraw the logo. Yellow
fill = to be completed by the team. Word / PDF / HTML outputs (when added) follow the same colours.

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
  Post ids are `(channel_id << 32) + n`; thread replies share that numbering but are hidden
  from the normal list. A message with replies has `thread_id == post_id` and
  `last_comment_at`; its replies come from `list` with `thread_id=<message>` and an anchor
  *before* the replies (not the message itself). Not an official Synology API.
- `integrations/synology_chat/watcher.py` – polls channels, saves shared files via
  `app/storage.channel_files_store()`, keeps progress in `STATE_DIR/chat_watch.json`.
- `python -m integrations.synology_chat.probe` – diagnostics against the real Chat NAS
  (message text and secrets are never printed).
