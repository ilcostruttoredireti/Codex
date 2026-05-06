# Gmail → HubSpot Contact Sync

Monitors all incoming Gmail messages and automatically upserts the senders as HubSpot contacts — no duplicates, fields filled in incrementally.

## How it works

```
Gmail Inbox
    │
    ▼ (polling every N seconds via Gmail History API)
Sender extraction
    ├─ email address
    ├─ first name / last name (from display name)
    └─ company domain
    │
    ▼
HubSpot search (by email — unique key)
    ├─ Not found → Create contact
    └─ Found     → Update missing fields only
    │
    ▼
Timeline note logged on the contact
Gmail message labelled "HubSpot-Synced"
    │
    ▼
Console output:
  ✅ Creato   / 🔄 Aggiornato / ⏭️ Ignorato
  Email | HubSpot contact ID
```

## Setup

### 1. Clone & install

```bash
git clone <repo-url>
cd <repo>
pip install -r requirements.txt
```

### 2. HubSpot Private App token

1. Go to **HubSpot → Settings → Integrations → Private Apps**
2. Create an app with scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.notes.write`
3. Copy the token

### 3. Gmail OAuth2 credentials

1. Go to **Google Cloud Console → APIs & Services → Credentials**
2. Create an **OAuth 2.0 Client ID** (Desktop application)
3. Download the JSON file and save it as `credentials.json` in the project root
4. Enable the **Gmail API** for your project

### 4. Environment variables

```bash
cp .env.example .env
# Edit .env and fill in HUBSPOT_ACCESS_TOKEN
```

| Variable | Default | Description |
|---|---|---|
| `HUBSPOT_ACCESS_TOKEN` | _(required)_ | HubSpot Private App token |
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | OAuth2 credentials from Google |
| `GMAIL_TOKEN_FILE` | `token.json` | Cached OAuth2 token (auto-created) |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between Gmail polls |
| `GMAIL_PROCESSED_LABEL` | `HubSpot-Synced` | Gmail label applied after processing |

### 5. First run (browser auth)

```bash
python main.py
```

On the first run a browser window opens for Gmail OAuth consent. After that the token is cached in `token.json`.

## Running continuously

```bash
# Simple foreground
python main.py

# As a background service (systemd example)
# See docs/systemd.md
```

## Fields synced to HubSpot

| HubSpot property | Source |
|---|---|
| `email` | Gmail From header |
| `firstname` | Display name (first word) |
| `lastname` | Display name (remaining words) |
| `company` | Derived from email domain (non-freemail) |
| `leadsource` | `"Gmail"` (fixed) |
| `hs_lead_status` | `"NEW"` (on creation) |

## Testing

```bash
pip install pytest
pytest test_sync.py -v
```

## Output format

```
2025-01-15 10:23:01  INFO     ✅  Stato: Creato      Email: mario.rossi@acme.com              ID HubSpot: 12345678
2025-01-15 10:23:02  INFO     🔄  Stato: Aggiornato  Email: giulia@startup.io                 ID HubSpot: 87654321
2025-01-15 10:23:03  INFO     ⏭️  Stato: Ignorato    Email: noreply@github.com                ID HubSpot: —
```
