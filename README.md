# Gmail → HubSpot Contact Sync

Monitors Gmail inbox for incoming emails, extracts sender contact data, and automatically syncs contacts to HubSpot CRM — creating new records or updating existing ones, with full deduplication.

## Features

- Scans the Gmail inbox for recent messages (configurable lookback window)
- Skips automated/no-reply senders automatically
- Deduplicates by email address (email is the unique key)
- Checks HubSpot before acting: **creates** new contacts, **updates** incomplete existing ones
- Persists processed message IDs to avoid re-processing across runs
- Outputs a per-email report: **Creato / Aggiornato / Ignorato**

## Setup

### 1. Install dependencies

```bash
npm install
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in:

```bash
cp .env.example .env
```

| Variable | Where to get it |
|---|---|
| `GMAIL_CLIENT_ID` | Google Cloud Console → OAuth 2.0 Client IDs |
| `GMAIL_CLIENT_SECRET` | Same as above |
| `GMAIL_REFRESH_TOKEN` | Run the OAuth flow once (see below) |
| `HUBSPOT_ACCESS_TOKEN` | HubSpot → Settings → Integrations → Private Apps |

#### Gmail OAuth refresh token

1. Create an OAuth 2.0 Client in [Google Cloud Console](https://console.cloud.google.com/) with scope `https://www.googleapis.com/auth/gmail.readonly`
2. Run the one-time auth flow to generate a refresh token (use the [OAuth Playground](https://developers.google.com/oauthplayground/) or any OAuth tool)
3. Paste the refresh token into `.env`

#### HubSpot Private App

1. Go to HubSpot → Settings → Integrations → Private Apps
2. Create an app with scopes: `crm.objects.contacts.read`, `crm.objects.contacts.write`
3. Copy the access token into `.env`

## Usage

### One-shot sync (last 24 hours)

```bash
npm run sync
# or
node src/sync.js
```

### Continuous watch mode (polls every 15 min by default)

```bash
npm run watch
# or
POLL_INTERVAL_MINUTES=10 node src/watch.js
```

### Custom lookback window

```bash
SYNC_LOOKBACK_HOURS=48 node src/sync.js
```

## Output format

```
─────────────────────────────────────────────
  Gmail → HubSpot Sync Report
─────────────────────────────────────────────
  Creati:     2
  Aggiornati: 1
  Ignorati:   14
─────────────────────────────────────────────
  [Creato    ] riccardo@martes-ai.com [HS:12345678]
  [Aggiornato] info@seozoom.it [HS:87654321]
  [Ignorato  ] noreply@discord.com (automated sender)
─────────────────────────────────────────────
```

## HubSpot fields populated

| HubSpot Field | Source |
|---|---|
| `email` | Sender email address |
| `firstname` | Parsed from "From" header |
| `lastname` | Parsed from "From" header |
| `company` | Derived from email domain |
| `hs_lead_status` | Set to `NEW` on creation |

## Architecture

```
src/
  sync.js      — CLI entry point (one-shot)
  watch.js     — Polling entry point (continuous)
  sync-lib.js  — Core sync logic (exported for reuse)
  gmail.js     — Gmail API helpers & sender parsing
  hubspot.js   — HubSpot CRM API helpers
```
