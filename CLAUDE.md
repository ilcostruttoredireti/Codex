# Codex — Gmail → HubSpot Contact Sync

Automated sync: monitors the Gmail inbox, extracts press-contact data from
both direct and forwarded emails, and creates or updates contacts in HubSpot.

## How the automation works

This repository is driven by a **scheduled Claude Code session** that runs the
sync logic directly through the Gmail and HubSpot MCP tools — no local API
keys needed in that mode. The Python script (`gmail_hubspot_sync.py`) is the
standalone equivalent for running outside Claude Code.

### Scheduled session behaviour

Each run:
1. Reads the Gmail inbox for the past 24 h (`in:inbox newer_than:1d`).
2. For every thread:
   - **Forwarded emails** (sender = `redazione@latestata.it` or
     `cristian.mameli.editore@gmail.com`) → parses the original press-contact
     from the body (`Da "Name" email@domain` pattern).
   - **Direct emails** → uses the `From:` header.
3. Skips: own account addresses, Facebook/Google notifications, mailer-daemons,
   no-reply prefixes, duplicate emails within the same run.
4. For each unique contact email:
   - Searches HubSpot by email (exact match).
   - **Exists** → fills in any blank `firstname`, `lastname`, `company` fields.
   - **Does not exist** → creates a new contact with `leadsource = Gmail`.
5. Outputs a result table: **Creato / Aggiornato / Ignorato** per contact.
6. Sends a push notification with a summary (new contacts created / updated).

## Running the Python script standalone

```bash
pip install -r requirements.txt

# First run: authenticates via browser and saves token.json
export HUBSPOT_API_KEY="pat-xxx"
export GMAIL_CREDENTIALS_FILE="credentials.json"   # Google Cloud OAuth client secret
export LOOKBACK_DAYS="1"
python gmail_hubspot_sync.py
```

On subsequent runs the saved `token.json` is used automatically.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `HUBSPOT_API_KEY` | *(required)* | HubSpot Private App token |
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | Google OAuth client secret |
| `GMAIL_TOKEN_FILE` | `token.json` | Cached OAuth token (auto-created) |
| `LOOKBACK_DAYS` | `1` | How many days back to scan the inbox |

## HubSpot fields populated

| HubSpot property | Source |
|---|---|
| `email` | Sender address |
| `firstname` | Parsed from `From:` / forwarded `Da:` header |
| `lastname` | Parsed from `From:` / forwarded `Da:` header |
| `company` | Extracted from email domain (non-generic domains only) |
| `leadsource` | Hard-coded to `"Gmail"` |
| `hs_lead_status` | Set to `"NEW"` on creation |

## Skip rules

Emails are skipped when:
- Address belongs to own accounts (`OWN_EMAILS` list in the script).
- Domain is in `SKIP_DOMAINS` (facebookmail.com, googlemail.com, …).
- Local part starts with an automated prefix (noreply, mailer-daemon, …).
- Email was already processed in the same run (dedup by address).
