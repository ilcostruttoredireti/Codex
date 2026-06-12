# Gmail → HubSpot Contact Sync

Monitors your Gmail inbox and automatically syncs sender contacts to HubSpot — creating new contacts or updating existing ones, without duplicates.

## What it does

For every inbound email it:

1. Extracts the sender's **email**, **name**, and **company** (derived from the domain)
2. Searches HubSpot by email to check for an existing contact
3. **Creates** the contact if not found, or **updates** missing fields if it already exists
4. Logs an *Email Received* engagement on the contact's HubSpot timeline
5. Sets `leadsource = "Gmail"` on every contact it touches
6. Skips no-reply / notification senders automatically

Output per email processed:
```
Status (Creato / Aggiornato / Ignorato)   sender@example.com   HubSpot ID: 12345
```

---

## Setup

### 1. Install dependencies

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
# edit .env and fill in the values
```

### 3. Gmail credentials (Google Cloud Console)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a project → enable the **Gmail API**
3. Create **OAuth 2.0 credentials** (Desktop app) → download `credentials.json`
4. Place `credentials.json` in this directory (or set `GMAIL_CREDENTIALS_FILE` in `.env`)

On first run the script opens a browser for OAuth consent. The token is saved to `token.json` for subsequent runs.

### 4. HubSpot Private App token

1. In HubSpot: **Settings → Integrations → Private Apps → Create a private app**
2. Grant scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.engagements.write`
3. Copy the token into `HUBSPOT_ACCESS_TOKEN` in `.env`

---

## Usage

### Continuous mode (polls every N seconds)

```bash
python gmail_hubspot_sync.py
# or with a custom interval
python gmail_hubspot_sync.py --interval 120
```

### One-shot mode (process inbox once and exit)

```bash
python gmail_hubspot_sync.py --once
```

### All options

```
--credentials FILE   Gmail OAuth credentials JSON  (default: credentials.json)
--token FILE         OAuth token storage path       (default: token.json)
--interval SECONDS   Poll interval (continuous)     (default: 60)
--once               Run once and exit
```

---

## How duplicates are avoided

The email address is used as the unique key for every HubSpot lookup. The script also maintains a local file (`.processed_message_ids.json`) tracking which Gmail message IDs have already been processed, so a restart does not re-process old messages.

---

## Ignored senders

The following are skipped automatically to prevent noise:

- Domains: `noreply.github.com`, `mailer.notion.so`, `notifications.google.com`, etc.
- Local parts: `noreply`, `no-reply`, `donotreply`, `mailer-daemon`, `postmaster`

Add more in the `IGNORED_DOMAINS` / `IGNORED_LOCAL_PARTS` sets in `gmail_hubspot_sync.py`.
