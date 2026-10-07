# ACE — roadmap

Principle: give the employee one skill at a time, prove it on historical data, and only
then give it access to live systems. Read-only before read-write; human approval before
any action.

| Phase | Milestone | Status |
|---|---|---|
| 1 | One AI employee, one role (Financial Reporting Analyst), read-only | ✅ v0.1 |
| 2 | Private GitHub repo `MALodhi-AA/ACE`, `.env` outside Git, CI tests | ✅ v0.1 |
| 3 | Synology Chat: `status`, `ask`, chatbot + slash command | ✅ built (deploy pending) |
| 4 | First skill: Monthly MIS v1.0 | ✅ v0.1 |
| 4b | Runs on the HP server; NAS access like an employee (own `ace` account, per-client share permissions) | ✅ v0.3 |
| 4c | ACE as a channel member: own Chat account, saves files shared in its channels | ✅ v0.4 |
| 4d | Answers @ACE in channels; task assignments, reminders, daily digest to MA | ⏭ v0.5 |
| 4e | Monitoring: weekly staff summary, unanswered client queries, overdue items | planned v0.6 |
| 5 | Train MIS on real historical TBs without Tally (v1.1, v1.2 …) | ⏭ next |
| 6 | Tally connector, 100 % read-only (TB, P&L, BS, ledgers, vouchers, cost centres) | planned |
| 7 | Task engine: AI tasks + human tasks via the existing task bot; recheck on `/task done` | planned |
| 8 | More skills: GL Review, Balance Sheet Review, AP/AR Rec, Stock Audit, Internal Audit, VAT, CT, Fraud Analytics, Cash-flow Forecast | planned |

## Phase 5 — how to train the MIS skill

1. Take 3 closed months for one client where you already know the right answer.
2. Export each TB into the input template; run `mis <file>`.
3. Compare against your own MIS. Every difference becomes either a mapping fix
   (`coa_mapping.csv`), a threshold change (`skill.yaml`), a method change (`SKILL.md`)
   or a calculation change (`engine.py`) — and a CHANGELOG line with a new version.
4. Accept the skill for a client when three consecutive months need no correction.

## Phase 6 — Tally (design notes)

- TallyPrime exposes XML over HTTP (default port 9000) on the machine running Tally.
  The connector will live in `integrations/tally/` and implement only *read* requests
  (Trial Balance, Day Book, Ledger, Group Summary, Cost Centre reports).
- It will produce the same TB DataFrame as the Excel loader, so the MIS skill does not change.
- Company / period resolution: `mis Mara Sep 2026` → Tally company + date range.
- Connection details in `.env`; no Tally credentials in Git.

## Phase 7 — tasks (design notes)

- `app/tasks.py`: AI task records (`AI-YYMM-NNN`) with status, requester, skill, outputs.
- Exceptions marked "needs human action" create tasks in the existing task bot via its API.
- On `/task done`, the employee re-runs the relevant check and closes or re-opens.

## Later architecture

```text
                      ACE
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
      SKILLS          TOOLS          MEMORY
   MIS · GL ·      Tally · Odoo ·   client facts,
   Audit · VAT ·   MariaDB · Files  prior months
   CT                  │
        └──────────────┬──────────────┘
                       ▼
                  TASK ENGINE  ── permissions + audit log
                       │
                       ▼
                 Synology Chat
```

The employee framework (`app/`) is generic, so a second AI employee (e.g. an AP clerk)
can reuse it with its own `employee.yaml` and skills.
