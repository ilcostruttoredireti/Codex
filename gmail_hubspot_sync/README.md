# Gmail → HubSpot Contact Sync

Monitors the Gmail INBOX continuously and syncs every new sender as a HubSpot contact, avoiding duplicates.

## Features

- Polls Gmail using the **History API** (efficient — no re-reads of old messages)
- Extracts `email`, `first name`, `last name`, `company` (from domain) from every new sender
- **Creates** the contact in HubSpot when not found
- **Updates** only missing fields when the contact already exists
- Skips no-reply, newsletter, and internal addresses automatically
- Applies a configurable Gmail label (`HubSpot-Synced`) to processed messages
- Adds a **timeline note** in HubSpot for every new inbound email

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Gmail OAuth

1. Open [Google Cloud Console](https://console.cloud.google.com) → **APIs & Services → Credentials**
2. Create an **OAuth 2.0 Client ID** (Desktop app)
3. Download the JSON and save it as `credentials.json` in the project root
4. Enable the **Gmail API** for your project

On first run the browser will open for OAuth consent; the token is cached in `token.json`.

### 3. Configure HubSpot

1. In HubSpot go to **Settings → Integrations → Private Apps**
2. Create an app with scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.notes.write`
3. Copy the access token

### 4. Set environment variables

```bash
cp .env.example .env
# Edit .env and fill in HUBSPOT_ACCESS_TOKEN
```

### 5. Run

```bash
python main.py
```

## Output

Each processed email prints one line:

```
Creato       | mario.rossi@acme.com                     | ID: 12345
Aggiornato   | luigi@bigcorp.io                         | ID: 67890
Ignorato     | noreply@notifications.github.com         | ID: —  (Ignored prefix: noreply)
```

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `HUBSPOT_ACCESS_TOKEN` | — | HubSpot Private App token |
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | OAuth client secrets |
| `GMAIL_TOKEN_FILE` | `token.json` | Cached OAuth token |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between polls |
| `GMAIL_PROCESSED_LABEL` | `HubSpot-Synced` | Label applied after sync |

## Running Tests

```bash
pytest tests/ -v
```

## Project Structure

```
gmail_hubspot_sync/
├── main.py               # Entry point — polling loop
├── gmail_monitor.py      # Gmail History API wrapper
├── hubspot_client.py     # HubSpot create/update/search
├── contact_processor.py  # Orchestrates parse → decide → act
├── utils.py              # Email / name / domain helpers
├── config.py             # Env-var config + ignore lists
├── requirements.txt
├── .env.example
└── tests/
    ├── test_utils.py
    └── test_contact_processor.py
```
