# Changelog

## 0.5.2 - 2026-10-07
- Attendance database (step 1 of v0.6): read-only MariaDB client
  (`integrations/attendance/db.py`; read-only session, only SELECT/SHOW allowed) and a probe
  `python -m integrations.attendance.probe` that lists databases, tables, columns, row
  counts and date ranges - never personal values. Settings ATT_DB_HOST / PORT / USER /
  PASSWORD / NAME. New dependency: PyMySQL.

## 0.5.1 - 2026-10-07
- Follow-ups without a time now go out at the start of working hours (WORK_START, e.g.
  11:00) instead of a fixed 10:00, matching the attendance system's hours.

## 0.5.0 - 2026-10-07
Tasks and follow-ups - ACE as Sir Muhammad Ali's assistant.
- Instructions in plain words from Sir Muhammad Ali (ACE_ADMINS only), in his direct chat
  with ACE or with @ACE in a channel: "ask Ali to send the Mara VAT working by Thursday,
  follow up daily". ACE shows what it understood (task, person, private/channel, due
  date, follow-up, escalation, the exact message) and waits for OK / a correction / cancel.
- Delivery privately (needs one message from the person to ACE first) or in a channel
  (replies in the message's thread are read as replies to the task).
- Follow-ups on schedule within working hours (WORK_DAYS / WORK_START / WORK_END):
  default = on the due date at 10:00 then daily; or daily at a time, every N hours, none.
  "when he checks in" waits for working hours until attendance is connected (v0.6).
- Staff replies are understood: done (task closed, Sir Muhammad Ali informed), more time
  (he approves/rejects with `approve T-n` / `reject T-n`), blocked (he is told), update
  (noted), "tell Sir Muhammad Ali ..." (passed on). Other questions are answered as before.
- Overdue tasks and "tell me if not done by ..." are reported to him; morning digest on
  working days at DIGEST_TIME (due today, overdue, approvals waiting, blocked, completed,
  files saved); `digest` any time.
- Commands for him: tasks, task T-n, remind / close / cancel / pause / resume T-n.
- Task register in state/ace.db (SQLite) with a full history per task.

## 0.4.11 - 2026-10-07
- Titles for colleagues: `config/employee.yaml` now has a `titles:` list. ACE always
  writes "Sir Amir Hussain" (and "Sir Muhammad Ali"), in AI answers and in its own
  messages, and addresses the person it is talking to by their title.

## 0.4.10 - 2026-10-07
- ACE refers to the head of the firm as "Sir Muhammad Ali" everywhere: in answers (AI
  persona), in its own messages ("Only Sir Muhammad Ali can ...", "Ask Sir Muhammad Ali
  to ..."), in `status` ("Works for: Sir Muhammad Ali") and in help. The name is set
  once in `config/employee.yaml` (`manager: name:`).

## 0.4.9 - 2026-10-07
- Fix: an empty chat (e.g. a new direct chat with no messages yet) answered "post not
  found" (402) and the error stopped that whole round of checks, so other chats were
  checked less and less often. Each chat is now checked on its own, and an empty chat
  simply waits for its first message (which is then answered).
- Quieter logs: the NAS connection no longer writes a line for every file operation.

## 0.4.8 - 2026-10-07
- Fix: in some channels (e.g. PaymentTracker-MaraGroup) Chat returns nothing when asked
  for the first message without an anchor, so ACE thought the channel was empty
  ("from post 0", "checked 0"). ACE now asks from message number 1 onwards, and finds the
  newest message with a quick binary search (Chat answers "post not found" past the end).
- Fix: replies sent right after each other were refused by Chat ("create post too fast",
  e.g. the "Done" message of `collect files`). ACE now waits and retries.
- Every chat starts again from its newest message after this update.

## 0.4.7 - 2026-10-07
- Fix: Chat returns fewer messages per request than ACE asks for, and ACE took a short
  page as "end of history". `collect files` therefore only read the oldest messages of a
  busy channel (0 files for Oct 2026), and the starting point of big channels could be
  too early. ACE now keeps reading until Chat has nothing newer.
- After this update every chat starts again from its newest message (nothing old is
  answered or saved by itself).
- `collect files` reports how many messages it looked through and their date range.

## 0.4.6 - 2026-10-07
- `collect files` understands plain words: "collect files from "PaymentTracker-MaraGroup"
  for the month of Oct 2026", "... last month", "... on 5 October 2026",
  "... from 1st Sep 2026 to 30/09/2026", "save all files of <channel> for September 2026".
- Channel names match loosely (case, spaces, dashes and dots are ignored) and an unknown
  name gets a "Did you mean ...?" suggestion.

## 0.4.5 - 2026-10-07
- Shared files are now saved in day folders:
  `ACE/channel-files/<Channel>/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<file>`.
- New manager instruction `collect files [channel] [from YYYY-MM-DD] [to YYYY-MM-DD]`:
  ACE goes back through a channel's history, including replies in threads, and saves
  every file shared in that period. In a channel or thread: `@ACE collect files from ...`;
  in a direct chat with ACE: `collect files <channel> from ...`. Only users in
  `ACE_ADMINS` may use it. Files saved before are never saved twice. ACE replies with a
  summary (files, MB, already saved, skipped, failed).

## 0.4.4 - 2026-10-07
- Fix: when a thread that already had replies got a new one, ACE also answered the old
  replies. Replies from before ACE started following a thread are now always skipped.

## 0.4.3 - 2026-10-07
- Threads: ACE now also watches replies inside threads (Chat keeps them out of the
  normal message list). Files shared in a thread are saved like any other, and
  `@ACE` in a thread is answered in that thread. "Saved: ..." confirmations go into
  the file's thread.
- Threads on the last 30 messages of each channel are followed; replies that existed
  before ACE first looked are not back-filled.

## 0.4.2 - 2026-10-07
- Fix: direct chats with the ACE user were not seen. Synology Chat lists unnamed
  conversations as type "anonymous"; with 2 members it is a direct chat (ACE answers
  every message), with more members a group conversation (ACE answers when @mentioned
  and saves shared files).

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
