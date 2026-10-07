# Setting up ACE on the HP server

ACE runs as a Linux container in **Docker Desktop on the HP server** (Windows Server),
next to your existing `ocr-engine` stack. It works with two Synology machines over the LAN:

```text
 Staff ── Synology Chat (DS723+) ──POST /synology/bot──►  ACE container (HP server :8080)
                       ▲                                     │   │
                       └──── replies via Chat API ───────────┘   │ signs in over SMB as "ace"
                                                                 ▼
                       AA-RS / RS1619xs+ (192.168.2.142)
                         ├─ client shares (Food Box, FALA Hospitality, …)  read-only, as permitted in DSM
                         └─ ACE share (inbox/, reports/)                    read/write
                       Anthropic API (internet) ◄── commentary / questions
```

**ACE is given access like an employee.** It signs in to AA-RS with its own `ace` account and
sees exactly the shared folders that account is allowed to read. To give ACE a client, grant
`ace` *Read only* on that client's shared folder in DSM – no change to ACE is needed. To take
it away, set *No access*. The HP server's mapped network drives are not used.

Time needed: about 45 minutes.

## 1. On AA-RS (RS1619xs+): ACE's account and desk

1. **ACE's shared folder** – Control Panel → Shared Folder → Create `ACE`.
   In File Station create two folders inside it: `inbox` and `reports`.
   Enable it as a Drive **Team Folder** if staff should open reports in Synology Drive.
2. **User `ace`** – Control Panel → User & Group → Create:
   - Description: *AI employee service account – no interactive login. Owner: MA*
   - Password: long and random (store it in your password manager).
   - Groups: **users** only (never administrators).
   - Shared folders:
     - `ACE` → **Read/Write**
     - client shares ACE should work on → **Read only** (start with one or two)
     - everything else (backups, `AA Team`, `chat`, `docker`, `homes`, …) → **No access**
   - Applications: **Allow SMB only**, Deny everything else.
3. SMB must be enabled (Control Panel → File Services → SMB). SMB2 or SMB3 minimum is fine.
4. Recommended: reserve 192.168.2.142 for AA-RS in the router's DHCP (or set a fixed IP in DSM).

**Never use the NAS admin account for ACE.**

Giving ACE another client later: Control Panel → Shared Folder → select the client → Edit →
Permissions → `ace` → **Read only** → Save. ACE sees it within a minute.

## 2. On the DS723+ (Chat NAS): create the bot

1. Synology Chat → avatar → **Integration → Bots → Create**.
2. Name `ACE`, description `Financial Reporting Analyst`, icon `assets/ace-avatar.png`.
3. **Outgoing URL:** `http://<HP-server-LAN-IP>:8080/synology/bot`
4. Copy the **Token**. Save.

## 3. On the HP server: get the code

PowerShell (as your admin user):

```powershell
mkdir C:\ACE -Force
git clone https://github.com/MALodhi-AA/ACE.git C:\ACE\app
cd C:\ACE\app
copy .env.example .env
notepad .env
```

(Not inside OneDrive.) If Git is not installed: `winget install Git.Git`.

Fill in `.env`:

| Setting | Value |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys (set a monthly spend limit there) |
| `SYNOLOGY_BASE_URL` | `http://<DS723+ LAN IP>:5000` |
| `SYNOLOGY_BOT_TOKEN` | token from step 2 |
| `NAS_HOST` | `192.168.2.142` |
| `NAS_USER` / `NAS_PASSWORD` | `ace` and its password from step 1 |
| `ACE_SHARE` | `ACE` |
| `ALLOWED_USERS` | leave empty for the first test, then fill in (step 6) |

Protect the file – only administrators should read it:

```powershell
icacls C:\ACE\app\.env /inheritance:r /grant:r "Administrators:F" "SYSTEM:F"
```

## 4. Open the port for the Chat NAS only

```powershell
New-NetFirewallRule -DisplayName "ACE - Synology Chat bot" -Direction Inbound `
  -Protocol TCP -LocalPort 8080 -RemoteAddress <DS723+ LAN IP> -Action Allow
```

Do not forward 8080 on the router. If port 8080 is already used on the HP server,
set `ACE_PORT=8090` (for example) in `.env`, open that port instead and use it in the bot's Outgoing URL.

## 5. Build and start

```powershell
cd C:\ACE\app
docker compose up -d --build
docker compose ps
docker compose logs -f ace        # Ctrl+C to stop following
```

Check from the HP server: <http://localhost:8080/health> → `{"status":"ok", ...}`.
In Docker Desktop you will see a new **ace** stack next to `ocr-engine-main`.

## 6. First conversation

Message the ACE bot in Synology Chat:

1. `status` – Synology Chat ✓, AI Model ✓, Reports folder ✓ (ACE/reports), and under
   *File access*: `Drive NAS (192.168.2.142) as 'ace' ✓ - N client folders`.
2. `whoami` → add your user_id (and finance staff) to `ALLOWED_USERS` in `.env`, then
   `docker compose up -d` (re-reads `.env`).
3. `ask Explain EBITDA`
4. `files` → the client folders ACE can read; `files Food Box` → browse; then e.g.
   `mis Food Box/2026/09 Sep/TB.xlsx` (part of the file name is enough).
5. Open the report from `ACE/reports/<Company>/<Period>/` in Synology Drive.

## 7. ACE in channels (v0.4)

ACE can also be a member of Synology Chat channels, with its own Chat account
(separate from the bot). It saves every file shared in its channels to
`ACE/channel-files/<Channel>/<YYYY-MM>/` and replies under the file.

1. On the DS723+: Control Panel → User & Group → create `ace` (group `users`, no shared
   folders, **Applications: Synology Chat only**, no 2-step verification).
   Login Portal → Applications → Synology Chat → alias `chat`; sign in once at
   `https://<DS723+>:5001/chat` as `ace` and set the name *ACE* and the avatar.
2. In `.env`: `CHAT_USER=ace` and `CHAT_PASSWORD=...` (see `.env.example` for options).
3. `docker compose up -d --build`, then add ACE to channels like any colleague.
4. `status` shows *Channel files ✓ - watching N channels*.

ACE also answers through this account: send it a direct message, or write `@ACE ...`
in a channel (same commands as the bot). In channels it only reacts when mentioned.

ACE starts from the newest post when it first sees a channel (no old files). Remove ACE
from a channel, or list it in `CHAT_IGNORE_CHANNELS`, to stop it saving there.
Tell staff that ACE saves files shared in the channels it is in.

## Keeping it running after a reboot

Docker Desktop is a desktop app: containers only start once Docker Desktop is running,
which normally means after a user signs in. Your `ocr-engine` stack has the same dependency.

- Docker Desktop → Settings → General → tick **Start Docker Desktop when you sign in**.
- ACE uses `restart: unless-stopped`, so it comes back by itself whenever Docker Desktop starts.
- For unattended reboots, either sign in after maintenance, or later move the Docker
  workloads to Docker Engine in a Linux VM / WSL Ubuntu so they run without a sign-in.
- Docker Desktop's licence is free for small businesses (fewer than 250 employees **and**
  under USD 10 million annual revenue); otherwise a paid subscription is needed.

## Updating ACE

```powershell
cd C:\ACE\app
git pull
docker compose up -d --build
```

Changes only under `skills/` or `config/` need just `docker compose restart ace`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `status`: *NT_STATUS_LOGON_FAILURE* | `NAS_USER`/`NAS_PASSWORD` wrong, or SMB not allowed for `ace` (Applications) |
| `status`: *did not answer in time* | `NAS_HOST` wrong or AA-RS unreachable from the HP server |
| `files` shows no client folders | `ace` has no *Read only* permission on any client share yet |
| A client folder is missing from `files` | permission not granted, or the share is hidden from browsing – add it to `NAS_SHARES` |
| *couldn't save the report* | `ace` needs Read/Write on `ACE`, and `ACE/reports` must exist |
| Bot never answers | `docker compose logs ace`; firewall rule allows the DS723+ IP? Outgoing URL uses the HP server's LAN IP? |
| `401` in logs | Bot token in `.env` doesn't match the bot |
| `Synology Chat ✗` / replies not delivered | `SYNOLOGY_BASE_URL` must be the DS723+ LAN IP, reachable from the HP server |
| `Channel files: starting ✗ - Chat sign-in failed` | `CHAT_USER`/`CHAT_PASSWORD`, Synology Chat allowed for `ace` on the DS723+, no 2-step verification |
| A shared file was not saved | `docker compose logs ace`; ACE needs Read/Write on `ACE` (AA-RS); files over `CHAT_MAX_FILE_MB` are skipped |
| `AI Model ✗` | API key, spend limit, or outbound internet from the HP server |
