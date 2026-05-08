# Gmail → HubSpot Contact Sync

Monitors your Gmail inbox continuously and syncs every new sender as a contact in HubSpot. Avoids duplicates and updates existing records with missing fields.

## What it does

For every new incoming email it:

1. Extracts sender data — email, name, company (from domain)
2. Checks HubSpot for an existing contact (using email as unique key)
   - **Exists** → fills in any missing fields (name, company)
   - **Does not exist** → creates a new contact
3. Sets `Lead Source = Gmail` and logs an inbound email activity on the contact timeline
4. Prints a per-email summary:

```
────────────────────────────────────────────────────────────
  Stato            : Creato
  Email contatto   : mario.rossi@acme.com
  ID HubSpot       : 12345
  Nome             : Mario Rossi
  Azienda          : Acme
  Oggetto email    : Richiesta preventivo
```

## Setup

### 1. Google Cloud — enable Gmail API

1. Go to <https://console.cloud.google.com>
2. Create a project (or select an existing one)
3. Enable **Gmail API** under *APIs & Services → Library*
4. Create **OAuth 2.0 Client ID** credentials (Desktop app type)
5. Download the JSON file and save it as `gmail_credentials.json` in this directory

### 2. HubSpot — create a Private App

1. In HubSpot go to *Settings → Integrations → Private Apps*
2. Create a new app with at minimum these scopes:
   - `crm.objects.contacts.read`
   - `crm.objects.contacts.write`
   - `crm.objects.emails.write`
3. Copy the generated token

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in HUBSPOT_API_KEY
```

### 4. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 5. Run

```bash
python gmail_hubspot_sync.py
```

On first run a browser window opens for Google OAuth consent. After approving, a `gmail_token.json` is saved for future runs.

The script saves the last processed Gmail `historyId` to `sync_state.json` so it picks up exactly where it left off if stopped and restarted.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `HUBSPOT_API_KEY` | — | HubSpot Private App token (required) |
| `GMAIL_CREDENTIALS_FILE` | `gmail_credentials.json` | Google OAuth credentials |
| `GMAIL_TOKEN_FILE` | `gmail_token.json` | Saved OAuth token |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between inbox checks |
| `SKIP_DOMAINS` | `gmail.com,googlemail.com` | Domains never imported as contacts |
| `STATE_FILE` | `sync_state.json` | Persistence file for historyId |

## Output states

| State | Meaning |
|---|---|
| **Creato** | New contact created in HubSpot |
| **Aggiornato** | Existing contact updated with missing fields |
| **Ignorato** | Contact already complete — no changes needed |
