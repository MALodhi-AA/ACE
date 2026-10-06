# Setting up ACE on the HP server

ACE runs as a Linux container in **Docker Desktop on the HP server** (Windows Server),
next to your existing `ocr-engine` stack. It talks to two Synology machines over the LAN:

```text
 Staff ── Synology Chat (DS723+) ──POST /synology/bot──►  ACE container (HP server :8080)
                       ▲                                     │   │
                       └──── replies via Chat API ───────────┘   │ SMB (user "ace")
                                                                 ▼
                                      RS1619xs+  ─ Clients share  (read-only)
                                                 ─ ACE share      (inbox + reports, read/write)
                                      Anthropic API (internet) ◄── commentary / questions
```

Time needed: about 45 minutes.

## 1. On the RS1619xs+ (Drive NAS): ACE's account and folder

1. **Shared folder** – Control Panel → Shared Folder → Create `ACE`.
   Inside it (File Station) create `inbox` and `reports`.
   Enable it as a Drive **Team Folder** if staff should see reports in Synology Drive.
2. **User** – Control Panel → User & Group → Create `ace`:
   - Description: *AI employee service account – no interactive login. Owner: MA*
   - Password: long, **letters and digits only** (it goes into an SMB mount option).
   - Groups: **users** only.
   - Shared folders: `ACE` → **Read/Write**; client folder(s) (e.g. `Clients`) → **Read only**;
     everything else → **No access**.
   - Applications: **Allow SMB only**, Deny everything else.
3. Make sure SMB is on (Control Panel → File Services → SMB) with minimum version SMB2 or higher.

You can delete the `ACE` user/folder created earlier on the SA3600 – it is no longer used.

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
| `NAS_IP` | LAN IP of the RS1619xs+ |
| `NAS_USER` / `NAS_PASSWORD` | the `ace` account from step 1 |
| `CLIENTS_SHARE` | exact name of the client shared folder on the RS1619xs+ |
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

1. `status` – everything should be ✓ except Tally/Odoo/Finance DB/Task Management.
   "File sources" should show `Clients ✓` and `inbox ✓`.
2. `whoami` → add your user_id (and finance staff) to `ALLOWED_USERS` in `.env`, then
   `docker compose up -d` (re-reads `.env`).
3. `ask Explain EBITDA`
4. `files Clients` → browse to a client month, then e.g.
   `mis Clients/Mara/2026/09 Sep/TB.xlsx`
5. Open the report from `ACE/reports/...` in Synology Drive.

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
| Container exits at start with *mount error(13): Permission denied* | `NAS_USER`/`NAS_PASSWORD` wrong, or `ace` not allowed SMB on the RS1619xs+ |
| *mount error(2): No such file or directory* | `CLIENTS_SHARE` / `ACE_SHARE` name doesn't match the shared folder name exactly |
| Changed NAS settings in `.env` but nothing changes | Docker keeps SMB volume settings: `docker compose down`, `docker volume rm ace_clients ace_ace_share`, `docker compose up -d` |
| Bot never answers | `docker compose logs ace`; firewall rule allows the DS723+ IP? Outgoing URL uses the HP server's LAN IP? |
| `401` in logs | Bot token in `.env` doesn't match the bot |
| `Synology Chat ✗` / replies not delivered | `SYNOLOGY_BASE_URL` must be the DS723+ LAN IP, reachable from the HP server |
| `Clients ✗ unreachable` in status | RS1619xs+ offline or share renamed; ACE keeps answering other commands |
| `AI Model ✗` | API key, spend limit, or outbound internet from the HP server |
