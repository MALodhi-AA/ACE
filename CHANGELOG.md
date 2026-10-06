# Changelog

## 0.2.0 — 2026-10-06

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
