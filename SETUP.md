# Gmail → HubSpot Contact Sync — Setup Guide

## How it works

1. Polls Gmail for new inbox messages using the Gmail History API (incremental — only new messages per cycle)
2. Extracts sender info: email, name, company (inferred from domain)
3. Searches HubSpot for an existing contact by email
   - **New contact** → creates it with all available fields
   - **Existing contact** → fills in only blank fields (never overwrites richer data)
4. Creates a HubSpot Note activity linked to the contact for every email received
5. Prints per-email outcome: `Creato` / `Aggiornato` / `Ignorato`

---

## Prerequisites

- Python 3.10+
- A Google Cloud project with the Gmail API enabled
- A HubSpot account with a Private App token

---

## Step 1 — Google Cloud / Gmail API

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create or select a project
3. Enable the **Gmail API** (`APIs & Services → Enable APIs → Gmail API`)
4. Create **OAuth 2.0 credentials**:
   - `APIs & Services → Credentials → Create Credentials → OAuth client ID`
   - Application type: **Desktop app**
   - Download the JSON file
5. Place the file at `credentials/gmail_credentials.json`

---

## Step 2 — HubSpot Private App

1. In HubSpot: `Settings → Integrations → Private Apps → Create a private app`
2. Grant scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.notes.read`
   - `crm.objects.notes.write`
3. Copy the generated token

---

## Step 3 — Configuration

```bash
cp .env.example .env
```

Edit `.env`:

```env
HUBSPOT_ACCESS_TOKEN=pat-na1-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
GMAIL_CREDENTIALS_FILE=credentials/gmail_credentials.json
GMAIL_TOKEN_FILE=credentials/gmail_token.json
GMAIL_USER=me
POLL_INTERVAL=60
STATE_FILE=state/sync_state.json
LOG_LEVEL=INFO
```

---

## Step 4 — Install & Run

```bash
pip install -r requirements.txt

# First run: opens browser for Google OAuth2 consent
python main.py
```

On first run a browser window opens for Google authentication. After
consenting, the token is saved to `credentials/gmail_token.json` and
all subsequent runs are non-interactive.

---

## Output format (one line per email)

```
Stato: Creato    | Email: john@acmecorp.com  | ID HubSpot: 12345678
Stato: Aggiornato| Email: jane@startup.io    | ID HubSpot: 87654321 | Nota: Updated fields: company
Stato: Ignorato  | Email: noreply@sender.com | ID HubSpot: N/A       | Nota: Automated/no-reply sender
```

---

## Running as a background service (systemd)

```ini
# /etc/systemd/system/gmail-hubspot-sync.service
[Unit]
Description=Gmail → HubSpot Contact Sync
After=network.target

[Service]
WorkingDirectory=/path/to/Codex
ExecStart=/usr/bin/python3 /path/to/Codex/main.py
Restart=on-failure
EnvironmentFile=/path/to/Codex/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gmail-hubspot-sync
sudo journalctl -u gmail-hubspot-sync -f
```
