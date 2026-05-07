# Gmail → HubSpot Contact Sync

Monitors your Gmail inbox and automatically creates or updates HubSpot contacts for every new email sender, using **email** as the unique key to avoid duplicates.

## How it works

```
Gmail INBOX  ──► poll every N seconds
      │
      ▼
Extract sender (email, name, domain)
      │
      ├─ contact exists in HubSpot? ─► update missing fields  → status: Updated
      │
      └─ contact missing?           ─► create new contact     → status: Created
                                        add "Inbound Gmail" tag
                                        record engagement note
```

Each processed email prints a one-line result:

```
[+] CREATED   alice@acme.com         id=12345678
[~] UPDATED   bob@example.com        id=87654321
[=] IGNORED   carol@acme.com         id=11223344
[!] ERROR     dave@broken.com        error=...
```

---

## Setup

### 1 — Prerequisites

- Python 3.11+
- A Google Cloud project with the **Gmail API** enabled
- A HubSpot account with a **Private App** token

### 2 — Install dependencies

```bash
pip install -r requirements.txt
```

### 3 — Configure Gmail OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials.
2. Create an **OAuth 2.0 Client ID** (Desktop app).
3. Download the JSON file and save it as `credentials.json` in the project root.

### 4 — Configure HubSpot

1. In HubSpot go to **Settings → Integrations → Private Apps**.
2. Create a new Private App with scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.engagements.write` *(optional, for activity timeline)*
3. Copy the token.

### 5 — Set environment variables

```bash
cp .env.example .env
# Edit .env and fill in HUBSPOT_API_KEY
```

### 6 — Run

```bash
cd gmail_hubspot_sync
python main.py
```

On first run a browser window opens for Gmail OAuth consent. The token is saved to `token.json` and reused automatically.

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | OAuth client secret file |
| `GMAIL_TOKEN_FILE` | `token.json` | Saved OAuth token (auto-created) |
| `HUBSPOT_API_KEY` | *(required)* | HubSpot Private App token |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between Gmail checks |
| `STATE_FILE` | `sync_state.json` | Persists last-processed historyId |

---

## HubSpot fields populated

| HubSpot property | Source |
|---|---|
| `email` | Sender address |
| `firstname` | Display name (first word) |
| `lastname` | Display name (remaining words) |
| `company` | Derived from email domain (skipped for free providers) |
| `lead_source_detail` | Hard-coded `"Gmail"` |
| `hs_lead_status` | `"NEW"` (only on creation) |

> **Note:** `lead_source_detail` must exist as a custom contact property in your HubSpot portal. Rename it to match your own property API name if needed, then update `hubspot_client.py`.

---

## Project structure

```
.
├── gmail_hubspot_sync/
│   ├── config.py          # Env-based configuration dataclass
│   ├── gmail_client.py    # Gmail API auth + History-API polling
│   ├── hubspot_client.py  # HubSpot contacts + engagement API
│   ├── sync.py            # Polling loop, state persistence, dedup
│   └── main.py            # Entry point + logging setup
├── requirements.txt
├── .env.example
└── .gitignore
```
