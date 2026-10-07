# Changelog

## 0.4.1 - 2026-10-07
- ACE talks through its own Chat account too: it answers every direct message sent to
  the ACE user and every channel message that mentions @ACE, with the same commands as
  the bot (`status`, `ask`, `files`, `mis` ...). `ALLOWED_USERS` applies as before.
- Only new messages are answered (nothing from before ACE first saw the chat).
- ACE checks its chats every 5 seconds by default (`CHAT_POLL_SECONDS`).
- `status` shows the Chat account line: channels, direct chats, files saved and
  messages answered today.

## 0.4.0 - 2026-10-07
- ACE joins Synology Chat as a team member: with its own Chat account (`CHAT_USER` /
  `CHAT_PASSWORD`) it watches every channel it has been added to.
- Every file shared in those channels is saved, never overwritten, to
  `ACE/channel-files/<Channel>/<YYYY-MM>/` on the Drive NAS, and ACE replies under the
  file with where it was saved. Old files are not back-filled; files shared while ACE
  was offline are picked up after a restart (progress kept in `./state`).
- `status` shows the channel watcher (channels watched, files saved today, last check).
- New settings: `CHAT_WATCH`, `CHAT_POLL_SECONDS`, `CHAT_MAX_FILE_MB`,
  `CHAT_REPLY_ON_SAVE`, `CHAT_CHANNEL_NAMES`, `CHAT_IGNORE_CHANNELS`.

## 0.3.2 - 2026-10-07
- Synology Chat probe (`python -m integrations.synology_chat.probe`): signs in as ACE's own
  Chat user account and checks that channels, posts and shared files can be read. First
  step towards v0.4 (ACE as a channel member). Message text and secrets are never printed.
- New settings `CHAT_USER` / `CHAT_PASSWORD`.

## 0.3.1 - 2026-10-07
- Chat replies are plain text: Synology Chat does not render markdown, so ACE is told
  not to use it and any `**bold**`, `#` headings, backticks and `*` bullets are stripped
  before sending.

## 0.3.0 — 2026-10-07

ACE gets NAS access like an employee.

- ACE signs in to the Synology Drive NAS (AA-RS) over SMB as its own `ace` account and sees
  exactly the shared folders that account may read in DSM - one share per client. Granting or
  removing a client is a DSM permission change; no ACE config or restart needed.
- `files` lists the client folders ACE can read; `files Food Box/2026` browses;
  `mis Food Box/2026/09 Sep/TB.xlsx` reads straight from the client share.
- Inbox and reports live in ACE's own `ACE` share (`inbox/`, `reports/`); ACE writes nowhere else
  (enforced in code and by the account's DSM permissions).
- No Docker volumes / CIFS mounts any more: works the same in Docker Desktop on Windows.
- CSV input supported; clear NAS error messages (bad password, unreachable, no permission).
- New: `app/nas.py`, `app/storage.py`, `tools/smb_test_server.sh`, `docs/DEVELOPMENT.md`.
  SMB integration tests run against a real Samba server in GitHub Actions (36 tests).

## 0.2.0 — 2026-10-06 (not deployed; superseded by 0.3.0)

ACE moves to the HP server and reads client files directly from the Synology Drive NAS.

- **Named read-only file sources** (`ACE_SOURCES`): `files Clients/Mara/2026` browses,
  `mis Clients/Mara/2026/09 Sep/TB.xlsx` runs the MIS straight from the client folder.
  Paths with `..`, absolute paths, other drives/shares and non-Excel/CSV files are refused.
- `status` lists each file source with ✓/✗; unreachable shares no longer stop ACE starting.
- `INBOX_DIR`, `REPORTS_DIR`, `LOG_DIR` overrides; rotating `ace.log` next to the audit log.
- Main deployment: HP server (Windows Server + Docker Desktop). `docker-compose.yml` mounts the
  RS1619xs+ `Clients` share read-only and the `ACE` share read/write over SMB.
  Synology deployment kept as `docker-compose.synology.yml`.
- Container runs as non-root user `ace` (uid 1000). `tzdata` added for Windows hosts.
- New guide `docs/SETUP_HP_SERVER.md`; tests grow from 16 to 29.

## 0.1.0 — 2026-10-06

First version of ACE (Accountability's Chief Examiner), the AI employee of Accountability Accountants.

- FastAPI service with `/health`, `/synology/bot` (chatbot) and `/synology/slash` (slash command).
- Synology Chat client: token verification, chatbot API replies, incoming webhook replies,
  message chunking, rate limiting and retries.
- Employee profile (`config/employee.yaml`): role, read-only authority, persona, systems.
- Commands: status, help, skills, ask (with short conversation memory), files, mis, whoami, reset.
- Permissions via `ALLOWED_USERS`; JSON-lines audit log.
- Claude model layer (`app/llm.py`), model chosen in `.env`.
- Skill framework and **Monthly MIS skill v1.0** with Excel report in Accountability
  Accountants house style; reports versioned, never overwritten.
- MIS input template and fictional sample TB generator.
- Docker / Synology Container Manager deployment; GitHub Actions test workflow; 15 tests.
