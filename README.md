# Gmail → HubSpot Contact Sync

Monitors Gmail inbox continuously and syncs sender contacts into HubSpot — creating new contacts and updating existing ones, with no duplicates.

## How it works

```
Gmail inbox (poll every N seconds)
        │
        ▼
  Extract sender info
  (email, name, domain)
        │
        ▼
  Search HubSpot by email
        │
   ┌────┴────┐
exists?     not found
   │              │
   ▼              ▼
Update        Create
missing       contact
fields        (source=Gmail)
   │              │
   └────┬─────────┘
        ▼
  Log inbound email activity
  (Note engagement on contact)
```

**Output per email processed:**

| Status    | Meaning                                      |
|-----------|----------------------------------------------|
| `CREATED` | New contact added to HubSpot                 |
| `UPDATED` | Existing contact had blank fields filled in  |
| `IGNORED` | Contact exists, no missing fields to update  |

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

### 3. Gmail API credentials (Google Cloud Console)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → Enable the **Gmail API**
3. Create **OAuth 2.0 Client ID** credentials (Desktop application)
4. Download the JSON file, save as `credentials.json` (or set `GMAIL_CREDENTIALS_FILE`)

### 4. HubSpot Private App token

1. In HubSpot: **Settings → Integrations → Private Apps**
2. Create a new private app with scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.schemas.contacts.read`
   - `crm.schemas.contacts.write`  
   - `timeline` (for activity logging)
3. Copy the token into `HUBSPOT_ACCESS_TOKEN` in `.env`

### 5. Authenticate Gmail (first time only)

```bash
python -m gmail_hubspot_sync.setup_gmail
```

This opens a browser window for OAuth consent. The token is cached in `token.json`.

### 6. Start the sync daemon

```bash
python -m gmail_hubspot_sync.main
```

---

## Configuration (`.env`)

| Variable                | Default          | Description                                  |
|-------------------------|------------------|----------------------------------------------|
| `HUBSPOT_ACCESS_TOKEN`  | *(required)*     | HubSpot private app token                    |
| `GMAIL_CREDENTIALS_FILE`| `credentials.json` | Path to OAuth credentials from Google     |
| `GMAIL_TOKEN_FILE`      | `token.json`     | Path for cached OAuth token                  |
| `POLL_INTERVAL_SECONDS` | `60`             | How often to poll Gmail (seconds)            |
| `INITIAL_LOOKBACK_DAYS` | `1`              | Days to look back on the very first run      |

---

## HubSpot fields populated

| HubSpot property  | Source                                |
|-------------------|---------------------------------------|
| `email`           | Sender address                        |
| `firstname`       | From display name (first word)        |
| `lastname`        | From display name (remaining words)   |
| `company`         | Sender email domain (e.g. `acme.com`) |
| `hs_lead_source`  | `"Gmail"` (set on creation)           |

Existing field values are **never overwritten** — only blank fields are filled.

---

## State file

`sync_state.json` is created automatically and stores:
- Timestamp of the last poll (to avoid re-fetching old messages)
- IDs of already-processed messages (to avoid double-processing)

Delete this file to trigger a full re-scan from `INITIAL_LOOKBACK_DAYS` ago.

---

## Skipped senders

Automated senders are detected and skipped automatically:
- `noreply@*`
- `no-reply@*`
- `mailer-daemon@*`
- `postmaster@*`
