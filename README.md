# Gmail → HubSpot Contact Sync

Monitors the Gmail inbox for incoming emails and automatically creates or updates contacts in HubSpot, using the sender's email address as the unique deduplication key.

## What it does

For every new message in the inbox:
1. Extracts sender email, first/last name, and company (inferred from domain)
2. Skips automated/no-reply addresses
3. Searches HubSpot for an existing contact by email
   - **Not found** → creates a new contact (source: `Gmail`, tag: `Inbound Gmail`)
   - **Found** → updates any missing fields (firstname, lastname, company, lead source)
4. Prints a result row per message: `Creato / Aggiornato / Ignorato`

## Output format

```
Stato        | Email                              | HubSpot ID
─────────────|────────────────────────────────────|───────────
Creato       | nuovo@azienda.it                   | 123456789
Aggiornato   | info@seozoom.it                    | 811584337
Ignorato     | no-reply@youtube.com               | -
```

## Requirements

- Node.js 18+
- A HubSpot private app token with **contacts** read + write scopes
- A valid Google OAuth2 access token for the Gmail account

## Environment variables

| Variable | Description |
|---|---|
| `HUBSPOT_ACCESS_TOKEN` | HubSpot private app token |
| `GOOGLE_OAUTH_TOKEN` | Gmail OAuth2 access token |
| `LOOKBACK_SECONDS` | How far back to scan (default: `900` = 15 min) |

## Usage

```bash
# Single run (last 15 min)
HUBSPOT_ACCESS_TOKEN=xxx GOOGLE_OAUTH_TOKEN=yyy node gmail_hubspot_sync.js

# Scan last hour
LOOKBACK_SECONDS=3600 HUBSPOT_ACCESS_TOKEN=xxx GOOGLE_OAUTH_TOKEN=yyy node gmail_hubspot_sync.js
```

## Scheduling (cron — every 15 min)

```cron
*/15 * * * * HUBSPOT_ACCESS_TOKEN=xxx GOOGLE_OAUTH_TOKEN=yyy node /path/to/gmail_hubspot_sync.js >> /var/log/gmail_hubspot_sync.log 2>&1
```

## Logic diagram

```
Gmail inbox (last N seconds)
        │
        ▼
  Is sender automated?  ──yes──▶  Ignorato
        │ no
        ▼
  Already seen this email?  ──yes──▶  skip
        │ no
        ▼
  Search HubSpot by email
        │
    ┌───┴───┐
  found   not found
    │         │
    ▼         ▼
 Update     Create
 missing    contact
 fields     (source=Gmail)
    │         │
    ▼         ▼
 Aggiornato  Creato
```
