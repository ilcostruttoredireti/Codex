# Gmail → HubSpot Contact Sync

Monitors your Gmail inbox continuously and syncs every new sender as a contact in HubSpot CRM — deduplicating by email, updating missing fields, and logging a timeline note on each contact.

## What it does

For every incoming email:

| Step | Action |
|---|---|
| 1 | Reads Gmail inbox for un-processed messages |
| 2 | Extracts sender: email, name, company (from domain) |
| 3 | Searches HubSpot for an existing contact by email |
| **Found** | Updates any blank fields (name, company) and adds a timeline note |
| **Not found** | Creates a new contact with source `lead` + timeline note |
| 4 | Labels the Gmail thread `HubSpot-Synced` |
| 5 | Persists processed message IDs to avoid duplicates on restart |

Each processed email produces one of these outcomes:

- ✅ **Creato** — new contact created in HubSpot
- 🔄 **Aggiornato** — existing contact updated / note added
- ⏭ **Ignorato** — skipped (free-mail domain, noreply sender)
- ❌ **Errore** — API failure (logged, retried next run)

---

## Prerequisites

- Python 3.11+
- A **Google Cloud project** with the Gmail API enabled and an OAuth 2.0 credential (Desktop app)
- A **HubSpot private-app token** with `crm.objects.contacts.write` and `crm.objects.notes.write` scopes

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/ilcostruttoredireti/codex.git
cd codex
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# edit .env and fill in HUBSPOT_ACCESS_TOKEN
```

### 3. Gmail credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services → Credentials**
2. Create an **OAuth 2.0 Client ID** (Application type: Desktop)
3. Download the JSON and save it as `credentials.json` (or the path set in `GMAIL_CREDENTIALS_FILE`)

### 4. Authorise Gmail (first run only)

```bash
python main.py --once
```

A browser window opens for OAuth. After approval a `token.json` is saved locally — subsequent runs are fully headless.

---

## Usage

```bash
# Single pass (useful for cron jobs)
python main.py --once

# Daemon mode (polls every 60 s by default)
python main.py

# Daemon with custom interval
python main.py --interval 120

# Verbose debug output
python main.py --debug
```

Logs are written to both stdout and `sync.log`.

### Cron example

```cron
*/5 * * * * cd /path/to/codex && python main.py --once >> /var/log/gmail_hs_sync.log 2>&1
```

---

## HubSpot fields populated

| HubSpot property | Source |
|---|---|
| `email` | Sender address |
| `firstname` | Display name (first word) |
| `lastname` | Display name (remaining words) |
| `company` | Domain segment (e.g. `acme.com` → `Acme`) |
| `lifecyclestage` | `lead` (configurable via `NEW_CONTACT_LIFECYCLE`) |
| `hs_lead_status` | `NEW` |
| Timeline note | Subject, date, "Inbound Gmail" tag |

---

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `HUBSPOT_ACCESS_TOKEN` | — | **Required.** HubSpot private-app token |
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | Google OAuth credentials |
| `GMAIL_TOKEN_FILE` | `token.json` | Cached OAuth token |
| `POLL_INTERVAL_SECONDS` | `60` | Daemon sleep between passes |
| `STATE_FILE` | `.sync_state.json` | Processed-ID persistence |
| `SKIP_FREE_DOMAINS` | `gmail.com,yahoo.com,…` | Comma-separated domains to skip |
| `SKIP_NOREPLY` | `true` | Skip noreply/automated senders |
| `GMAIL_SYNCED_LABEL` | `HubSpot-Synced` | Gmail label applied after sync |
| `NEW_CONTACT_LIFECYCLE` | `lead` | HubSpot lifecycle stage for new contacts |
