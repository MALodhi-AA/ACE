# Changelog

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
