# Gmail → HubSpot Contact Sync

Monitors Gmail for incoming emails and automatically creates or updates HubSpot contacts from senders, with deduplication and incremental sync.

---

## How it works

```
Gmail Inbox
    │
    ▼  (Gmail API – History / search)
New emails polled every N seconds
    │
    ▼  Parse sender: name · email · domain
Skip automated senders (noreply, newsletters…)
    │
    ▼  Search HubSpot by email (unique key)
 ┌──┴──────────────────────────────┐
 │ Exists?                         │
 │  YES → patch only blank fields  │
 │  NO  → create new contact       │
 └─────────────────────────────────┘
    │
    ▼
Log: ✅ CREATO / 🔄 AGGIORNATO / ⏭ IGNORATO
```

HubSpot fields populated:
| Field | Source |
|-------|--------|
| `email` | From header |
| `firstname` | Display name (first word) |
| `lastname` | Display name (remaining words) |
| `company` | Derived from email domain |
| `hs_lead_source` | Hard-coded `"Gmail"` |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Gmail API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable **Gmail API**
3. Create **OAuth 2.0 Client ID** (Desktop app) → Download JSON
4. Rename it to `credentials.json` (or set `GMAIL_CREDENTIALS_FILE` in `.env`)
5. Run the one-time authorisation helper:

```bash
python setup_gmail_oauth.py
```

This opens a browser, asks you to grant read-only Gmail access, and saves `token.json`.

### 3. HubSpot Private App token

1. HubSpot → Settings → Integrations → **Private Apps** → Create app
2. Required scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`
3. Copy the token into `.env` as `HUBSPOT_ACCESS_TOKEN`

### 4. Configure `.env`

```bash
cp .env.example .env
# edit .env with your values
```

---

## Running

```bash
# Continuous polling (default: every 60 s)
python gmail_hubspot_sync.py

# Single pass, then exit
python gmail_hubspot_sync.py --run-once

# Custom polling interval (30 s)
python gmail_hubspot_sync.py --interval 30

# Clear saved state and start fresh
python gmail_hubspot_sync.py --reset-state
```

### Sample output

```
2026-06-11 10:00:01 [INFO] Starting continuous sync (interval: 60s). Press Ctrl+C to stop.
2026-06-11 10:00:03 [INFO] Processing 3 new message(s)…
2026-06-11 10:00:04 [INFO] ✅ CREATO       | mario.rossi@acmecorp.com               | HubSpot ID: 12345
2026-06-11 10:00:05 [INFO] 🔄 AGGIORNATO   | giulia.bianchi@techstartup.io          | HubSpot ID: 67890
2026-06-11 10:00:05 [INFO] ⏭  IGNORATO    | newsletter@mailchimp.com               | (automated/skipped)
```

---

## Running as a service (Linux systemd)

```ini
# /etc/systemd/system/gmail-hubspot-sync.service
[Unit]
Description=Gmail → HubSpot Contact Sync
After=network.target

[Service]
WorkingDirectory=/path/to/codex
ExecStart=/usr/bin/python3 /path/to/codex/gmail_hubspot_sync.py
Restart=on-failure
EnvironmentFile=/path/to/codex/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now gmail-hubspot-sync
sudo journalctl -u gmail-hubspot-sync -f
```

---

## File reference

| File | Purpose |
|------|---------|
| `gmail_hubspot_sync.py` | Main sync script |
| `setup_gmail_oauth.py` | One-time OAuth2 authorisation |
| `requirements.txt` | Python dependencies |
| `.env.example` | Environment variable template |
| `sync_state.json` | Auto-generated runtime state (gitignored) |
| `token.json` | Auto-generated Gmail OAuth token (gitignored) |

---

## Security notes

- `token.json` and `.env` contain secrets — never commit them. Both are in `.gitignore`.
- The Gmail scope used is **read-only** (`gmail.readonly`): the script cannot send, delete, or modify emails.
- HubSpot token is stored only in `.env`; rotate it from the HubSpot Private Apps dashboard.
