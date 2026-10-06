# ACE — Accountability's Chief Examiner

**ACE** (Accountability's Chief Examiner) is the AI employee of Accountability Accountants, reachable through Synology Chat.

| | |
|---|---|
| **Version** | 0.2.0 |
| **Role** | Financial Reporting Analyst |
| **Authority** | Read-only (never posts or edits entries in any system) |
| **Skills** | Monthly MIS v1.0 |
| **Runs on** | HP server (Windows Server + Docker Desktop); Synology alternative kept |
| **AI model** | Anthropic Claude (configurable in `.env`) |

## How it works

```text
Synology Chat on DS723+ (bot DM or /ace slash command)
        │  HTTP POST over the LAN (token verified)
        ▼
ACE  (Docker container on the HP server, port 8080)
        ├── Employee      config/employee.yaml   identity, role, authority, persona
        ├── Permissions   ALLOWED_USERS          who may instruct it
        ├── Skills        skills/<skill>/        versioned procedures + code
        ├── AI model      app/llm.py             commentary and Q&A only, never arithmetic
        ├── Audit log     logs/audit.jsonl       every request, skill run and model call
        ├── File sources  RS1619xs+ Clients     read-only over SMB (user "ace")
        └── Reports  ──►  RS1619xs+ ACE/reports/<Company>/<Period>/  (visible in Synology Drive)
```

The design rule is **AI Employee → Tools + Skills + Tasks + Permissions + Audit Log**, not
"bot + giant prompt". Figures are calculated in Python and are reproducible; the model
writes commentary from those figures.

## Chat commands

Send these to the bot in a direct message, or after `/ace` in a channel:

| Command | What it does |
|---|---|
| `status` | Identity, role, skills, connected systems |
| `help` | Command list |
| `skills` | Skills with versions |
| `ask <question>` | Ask a finance/accounting question (anything not recognised is also treated as a question) |
| `files [folder]` | Browse the read-only file sources, e.g. `files Clients/Mara/2026` |
| `mis <file>` | Run the Monthly MIS skill, e.g. `mis Clients/Mara/2026/09 Sep/TB.xlsx` (partial file names work) |
| `whoami` | Shows your Chat user id (for `ALLOWED_USERS`) |
| `reset` | Clears conversation memory |

## Repository layout

```text
ace/
├── app/                      core service: web endpoints, employee, model layer, audit
├── integrations/
│   └── synology_chat/        chatbot + slash command + incoming webhook client
├── skills/
│   ├── base.py, registry.py  skill framework
│   └── monthly_mis/
│       ├── SKILL.md          the procedure (your method, in writing)
│       ├── skill.yaml        version, MIS lines, review thresholds, keywords
│       ├── CHANGELOG.md      training record - every correction becomes a version
│       ├── engine.py         deterministic calculations and checks
│       ├── commentary.py     AI commentary (rule-based fallback)
│       ├── report.py         Excel report in Accountability Accountants house style
│       └── skill.py          orchestration
├── config/
│   ├── employee.yaml         job description
│   └── coa_mapping.csv       account → MIS line mapping (optional)
├── assets/ace-avatar.png     bot icon for Synology Chat
├── templates/
│   └── MIS_Input_Template.xlsx
├── tests/                    pytest suite (runs on every push via GitHub Actions)
├── tools/make_sample_data.py generates the template and a fictional sample TB
├── docs/                     setup guide and roadmap
├── Dockerfile
├── docker-compose.yml        HP server (Docker Desktop, SMB volumes to the RS1619xs+)
├── docker-compose.synology.yml  alternative: run on a Synology NAS
└── .env.example              copy to .env (never committed)
```

## Run locally (Windows/Mac/Linux, for development)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on Mac/Linux)
pip install -r requirements-dev.txt
copy .env.example .env            # then fill in values
python tools/make_sample_data.py  # creates template + sample TB in data/inbox
pytest                            # 29 tests, no API key or NAS needed
uvicorn app.main:app --port 8080
```

## Deploy

Main: **[docs/SETUP_HP_SERVER.md](docs/SETUP_HP_SERVER.md)** (HP server, Docker Desktop).
Alternative: [docs/SETUP_SYNOLOGY.md](docs/SETUP_SYNOLOGY.md) (Synology Container Manager).

## Teaching the employee (improving a skill)

1. Run `mis <file>` on a real historical month and review the Excel report.
2. Write down what is wrong, e.g. *"Food cost should exclude staff meals GL 5105."*
3. Change `SKILL.md` (method), `skill.yaml` (thresholds/keywords) or `engine.py` (calculation),
   bump `version` in `skill.yaml`, and add a line to the skill's `CHANGELOG.md`.
4. Add a test that proves the fix, commit, push, redeploy.
5. Repeat until you trust the output. That is the employee's training — no model training needed.

## Security

- `.env` holds all secrets and is excluded by `.gitignore` and `.dockerignore`.
- Every inbound Chat request is token-verified (constant-time compare); bad tokens get 401.
- `ALLOWED_USERS` limits who can instruct the employee; refusals are logged.
- Client files are mounted read-only and the `ace` NAS account only has read permission on them;
  ACE can only open Excel/CSV files inside its named sources (no `..`, absolute paths or other shares).
- The container runs as a non-root user.
- Reports are never overwritten (`_v1`, `_v2`, …).
- v0.1 has no write access to any accounting system.
