# Alternative: running ACE on a Synology NAS

> The main deployment is the HP server - see [SETUP_HP_SERVER.md](SETUP_HP_SERVER.md).
> Use this guide only if ACE should run on a Synology instead, with
> `docker compose -f docker-compose.synology.yml up -d --build` (or a Container Manager
> project pointing at that file).

Time needed: about 45–60 minutes the first time.

## 0. Check the NAS can run containers

DSM → **Control Panel → Info Center**: note the exact model and DSM version.
Container Manager needs an **x86-64 (Intel/AMD) model on DSM 7.2 or later** — the
XS/XS+ business models (e.g. DS3622xs+, RS3621xs+) qualify. Open **Package Center**
and install **Container Manager** (and **Git Server** if you want to pull from GitHub
over SSH). The app needs about 300 MB RAM.

Running it on the NAS instead of the HP server is a good choice: Chat, Drive and the
employee live on the same box, there is one less server to maintain, and the files
never leave the NAS.

## 1. Create the shared folder (Synology Drive)

1. Control Panel → **Shared Folder** → Create `ACE`.
2. Inside it create three folders: `inbox`, `reports`, `logs`.
3. Synology Drive Admin Console → **Team Folder** → enable `ACE` so you (and
   accountants you choose) can drop TB files into `inbox` and open reports from `reports`
   on your PC.
4. Permissions: only finance staff get read/write; `logs` should be MA-only.

## 2. Create the GitHub repository

1. On GitHub: **New repository** → name `ace` → **Private** → no README
   (this repo already has one).
2. On your PC, in the `ace` folder:

```bash
git init
git add .
git commit -m "ACE v0.1.0"
git branch -M main
git remote add origin https://github.com/<your-account>/ace.git
git push -u origin main
```

Check on GitHub that **`.env` is not there** and that the *tests* workflow is green
(Actions tab).

## 3. Put the code on the NAS

SSH to the NAS (Control Panel → Terminal & SNMP → enable SSH), then:

```bash
sudo mkdir -p /volume1/docker && cd /volume1/docker
sudo git clone https://github.com/<your-account>/ace.git
cd ace
sudo cp .env.example .env
id <your-dsm-username>          # note uid and gid for PUID / PGID in .env
```

Private repo: create a GitHub **fine-grained token** with read-only *Contents* access to
this one repository and use it as the password when cloning.

(No SSH? Copy the folder to `/volume1/docker/ace` with File Station instead.)

## 4. Create the Synology Chat bot

1. Open Synology Chat → your avatar (top right) → **Integration** → **Bots** → **Create**.
2. Name: `ACE` · Description: `Financial Reporting Analyst` · Icon: upload `assets/ace-avatar.png`.
3. **Outgoing URL**: `http://<NAS-LAN-IP>:8080/synology/bot`
4. Copy the **Token** shown on the bot page.
5. Save. Users will find the bot under **Bots** in the left panel and chat to it by direct message.

Optional — keep your existing slash command style in channels:

- **Integration → Slash Commands → Create**: command `ace`, URL
  `http://<NAS-LAN-IP>:8080/synology/slash`; copy its token.
- **Integration → Incoming Webhooks → Create** for the finance channel; copy the webhook URL.
  (Long-running results in slash mode are posted to this channel.)

## 5. Fill in `.env`

```bash
sudo vi /volume1/docker/ace/.env
```

| Setting | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from console.anthropic.com → API keys (set a monthly spend limit there) |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` (default) |
| `SYNOLOGY_BASE_URL` | `http://<NAS-LAN-IP>:5000` — **not** `localhost` (that is the container itself) |
| `SYNOLOGY_BOT_TOKEN` | token from step 4 |
| `SYNOLOGY_SLASH_TOKEN`, `SYNOLOGY_INCOMING_WEBHOOK_URL` | only if using the slash command |
| `ALLOWED_USERS` | leave empty for the first test, then fill in (see step 7) |
| `PUID`, `PGID` | from `id` in step 3 |

Restrict the file: `sudo chmod 600 .env`.

## 6. Start the container

Container Manager → **Project** → **Create**:

- Project name: `ace`
- Path: `/volume1/docker/ace`
- Source: *Use existing docker-compose.yml*
- Build and start.

Or over SSH: `cd /volume1/docker/ace && sudo docker compose up -d --build`

Check: open `http://<NAS-LAN-IP>:8080/health` in a browser → `{"status":"ok", ...}`.

If the firewall is on: Control Panel → Security → Firewall → allow TCP 8080 **from the LAN
only**. Do not forward 8080 on the router; Chat reaches it internally.

## 7. First conversation (milestone: "employee hired")

In Synology Chat, message the bot:

```text
status
```

Expected:

```text
ACE - Accountability's Chief Examiner  v0.1.0
Status: Online
Role:
Financial Reporting Analyst
Available Skills:
1. Monthly MIS  v1.0
Connected Systems:
Synology Chat ✓
AI Model ✓ (claude-sonnet-5-5)
Synology Drive (reports) ✓
Tally ✗
Odoo ✗
Finance DB ✗
Task Management ✗
```

Then:

1. `whoami` → put your user_id (and those of finance staff) in `ALLOWED_USERS`, restart the project.
2. `ask Explain EBITDA`
3. Copy `templates/MIS_Input_Template.xlsx` (or the generated sample TB) into
   `ACE/inbox`, then `files` and `mis sample`.
4. Open the report from `ACE/reports/...` in Synology Drive.

## Updating

```bash
cd /volume1/docker/ace
sudo git pull
sudo docker compose up -d --build      # or Container Manager → Project → Build
```

Skill-only changes (`skills/`, `config/`) are mounted into the container, so a **restart**
is enough.

## Troubleshooting

| Symptom | Check |
|---|---|
| Bot never answers | `sudo docker logs ace`; is the Outgoing URL reachable from the NAS? Is `SYNOLOGY_BASE_URL` the LAN IP? |
| `401` in logs | Token in `.env` does not match the bot/slash-command token |
| "Chatbot not configured" | `SYNOLOGY_BASE_URL` or `SYNOLOGY_BOT_TOKEN` empty |
| SSL errors | Using `https://...:5001` with a self-signed cert → use `http://...:5000` on the LAN or set `SYNOLOGY_VERIFY_SSL=false` |
| `AI Model ✗` | API key, spend limit or outbound internet from the NAS |
| Permission denied writing reports | `PUID`/`PGID` must own `ACE/reports` and `logs` |
