# Gmail → HubSpot Contact Sync

Scheduled Claude Code routine that monitors incoming Gmail emails and syncs
sender contacts to HubSpot automatically.

## What it does

Each run:
1. Fetches recent inbox threads from Gmail
2. Skips already-processed thread IDs (tracked in `state.json`)
3. Extracts sender contact data (email, name, company from domain)
4. For forwarded emails, also parses the original sender from the body
5. Searches HubSpot for each email — if found, updates missing fields; if not, creates a new contact
6. Sets `hs_lead_source = "Gmail"` on every contact
7. Persists processed thread IDs and stats to `state.json`

## Files

| File | Purpose |
|------|---------|
| `gmail_hubspot_sync.py` | Standalone Python sync script |
| `requirements.txt` | Python dependencies |
| `state.json` | Processed thread IDs + cumulative stats |
| `logs/sync_YYYY-MM-DD.log` | Daily log files |
| `gmail_credentials.json` | OAuth2 app credentials (not committed) |
| `gmail_token.json` | OAuth2 token (not committed) |

## Running standalone

```bash
pip install -r requirements.txt
export HUBSPOT_ACCESS_TOKEN="your-private-app-token"
python gmail_hubspot_sync.py
```

## Senders ignored

- `redazione@latestata.it` (internal forwarding relay)
- `cristian.mameli.editore@gmail.com` (self)
- `pubblica.latestata@gmail.com` (own alias)
- Facebook notification addresses
- Generic noreply/mailer-daemon addresses

## HubSpot fields populated

| HubSpot Property | Source |
|-----------------|--------|
| `email` | Sender address |
| `firstname` | First word of display name |
| `lastname` | Remaining words of display name |
| `company` | Inferred from email domain |
| `hs_lead_source` | Fixed: `"Gmail"` |

## State file format

```json
{
  "last_sync": "2026-06-14T10:00:00+00:00",
  "processed_thread_ids": ["19ec...", "19eb..."],
  "stats": {
    "created": 12,
    "updated": 3,
    "ignored": 5
  }
}
```
