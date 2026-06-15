# Gmail → HubSpot Contact Sync

Monitors incoming Gmail messages and upserts sender contacts in HubSpot, avoiding duplicates and enriching existing records.

## What it does

For every new email received in the inbox (last 24 h by default):

1. Extracts the sender's **email**, **name**, and **company domain**
2. Handles forwarded messages by parsing the embedded `Da:` / `From:` line
3. Searches HubSpot for the contact by email (unique key)
   - **New contact** → creates it with all available fields
   - **Existing contact** → patches only the fields that are currently empty
4. Sets `hs_analytics_source_data_1 = "Gmail"` as the contact source
5. Skips internal/self addresses automatically

## Output per processed email

| Field | Values |
|-------|--------|
| Stato | `Creato` / `Aggiornato` / `Ignorato` / `Errore` |
| Email contatto | sender email address |
| ID contatto HubSpot | numeric HubSpot record ID |

## Setup

```bash
pip install -r requirements.txt
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `HUBSPOT_ACCESS_TOKEN` | HubSpot Private App token (Contacts read/write scope) |
| `GOOGLE_CREDENTIALS` | Path to Gmail OAuth2 `credentials.json` (default: `credentials.json`) |

On first run a browser window opens for Gmail OAuth consent; the token is saved to `token.json` for subsequent runs.

## Usage

```bash
# Sync last 24 h (default)
python gmail_hubspot_sync.py

# Sync last 48 h
python gmail_hubspot_sync.py --hours 48

# Dry run — print actions without writing to HubSpot
python gmail_hubspot_sync.py --dry-run
```

## Scheduled execution (cron)

```cron
# Every hour
0 * * * * cd /path/to/Codex && python gmail_hubspot_sync.py --hours 1 >> sync.log 2>&1
```

## Self-address exclusions

Addresses listed in `SELF_ADDRESSES` inside the script are never synced. Edit that set to match your own addresses:

```python
SELF_ADDRESSES: set[str] = {
    "cristian.mameli.editore@gmail.com",
    "redazione@latestata.it",
    "pubblica.latestata@gmail.com",
}
```
