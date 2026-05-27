# Gmail → HubSpot Contact Sync

Monitors Gmail inbox for new emails and automatically creates or updates
HubSpot CRM contacts from sender information — no duplicates.

## How it works

```
Gmail (new message)
  └─► Extract sender (email, name, company domain)
        └─► HubSpot search by email
              ├─ Found   → update only blank fields  →  [Aggiornato]
              └─ Missing → create new contact        →  [Creato]
```

Each processed email prints one of:
- **Creato** — new contact added to HubSpot
- **Aggiornato** — existing contact enriched with missing data
- **Ignorato** — contact already complete, nothing changed
- **Errore** — API error (details logged)

---

## Setup

### 1. Google Cloud — Gmail API

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create (or select) a project.
2. Enable the **Gmail API**.
3. Create **OAuth 2.0 credentials** (Desktop application type).
4. Download `credentials.json` and place it in this directory.

### 2. HubSpot — Private App

1. In HubSpot go to **Settings → Integrations → Private Apps**.
2. Create a new app with the scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
3. Copy the **Access Token**.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in HUBSPOT_ACCESS_TOKEN
```

### 4. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 5. First run (OAuth login)

The first execution opens a browser window for Google login.  
The token is saved to `token.json` and reused automatically.

```bash
python main.py --once
```

### 6. Continuous monitoring

```bash
python main.py
```

Press **Ctrl+C** to stop gracefully.

---

## Running as a service (systemd)

```ini
# /etc/systemd/system/gmail-hubspot-sync.service
[Unit]
Description=Gmail → HubSpot Contact Sync
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/gmail_hubspot_sync
ExecStart=/path/to/.venv/bin/python main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gmail-hubspot-sync
sudo journalctl -fu gmail-hubspot-sync
```

---

## Running with cron (single shot)

```cron
# Every 5 minutes
*/5 * * * * cd /path/to/gmail_hubspot_sync && .venv/bin/python main.py --once >> sync.log 2>&1
```

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | Google OAuth client secrets file |
| `GMAIL_TOKEN_FILE` | `token.json` | Cached OAuth token (auto-generated) |
| `HUBSPOT_ACCESS_TOKEN` | *(required)* | HubSpot Private App token |
| `POLL_INTERVAL_SECONDS` | `300` | Seconds between Gmail checks |
| `STATE_FILE` | `sync_state.json` | Stores last processed Gmail historyId |

---

## HubSpot fields populated

| HubSpot property | Source |
|---|---|
| `email` | Sender address |
| `firstname` | Display name (first word) |
| `lastname` | Display name (remaining words) |
| `company` | Derived from email domain |
| `leadsource` | Fixed: `"Gmail"` |
| `hs_content_membership_notes` | Fixed: `"Inbound Gmail"` |
