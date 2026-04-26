# Gmail → HubSpot Contact Sync

Monitors incoming Gmail messages and automatically creates or updates contacts in HubSpot, avoiding duplicates.

## How it works

```
Gmail inbox  ──→  parse sender  ──→  HubSpot search
                                         │
                               contact exists?
                               ├─ YES → update missing fields
                               └─ NO  → create new contact
                                         │
                                   log timeline note
```

For every processed email the script prints one of three statuses:

| Status    | Meaning                                      |
|-----------|----------------------------------------------|
| `CREATED` | New HubSpot contact created                  |
| `UPDATED` | Existing contact had missing fields filled in |
| `IGNORED` | Contact exists and all fields are already set |

## Setup

### 1. Google Cloud credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → *APIs & Services* → *Credentials*.
2. Create an **OAuth 2.0 Client ID** (Desktop app).
3. Download the JSON file and save it as `credentials.json` in this directory.
4. Enable the **Gmail API** for the project.

### 2. HubSpot private app token

1. HubSpot → *Settings* → *Integrations* → *Private Apps* → *Create a private app*.
2. Scopes needed: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `engagements.create`.
3. Copy the token.

### 3. Environment

```bash
cp .env.example .env
# edit .env and fill in HUBSPOT_API_KEY
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Run

```bash
# Continuous mode (polls every 60 s by default)
python main.py

# Single run, then exit
python main.py --once
```

On first run a browser window opens for Gmail OAuth consent. The token is cached in `token.json` for subsequent runs.

## HubSpot fields populated

| HubSpot property | Source                             |
|------------------|------------------------------------|
| `email`          | From address                       |
| `firstname`      | Display name (first word)          |
| `lastname`       | Display name (remaining words)     |
| `company`        | Derived from email domain (non-free providers) |
| `lead_source`    | Hard-coded `"Gmail"`               |

A **Note engagement** is also created on the contact's timeline for every inbound email.

## Configuration (`.env`)

| Variable                | Default         | Description                        |
|-------------------------|-----------------|------------------------------------|
| `GMAIL_CREDENTIALS_FILE`| `credentials.json` | OAuth client secret file        |
| `GMAIL_TOKEN_FILE`      | `token.json`    | Cached OAuth token                 |
| `HUBSPOT_API_KEY`       | —               | HubSpot private app token          |
| `POLL_INTERVAL_SECONDS` | `60`            | Seconds between inbox checks       |
| `GMAIL_QUERY`           | `in:inbox`      | Gmail search query filter          |
