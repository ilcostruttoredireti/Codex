# Gmail → HubSpot Contact Sync

Monitors Gmail inbox and automatically syncs sender contacts to HubSpot.

## What it does

For every new email in the inbox (last 24 h by default):
1. Extracts sender email, name, and company domain
2. Searches HubSpot by email (deduplication key)
3. **Creates** a new contact if not found
4. **Updates** a contact's missing fields if already present
5. **Skips** no-reply / automated senders and exact duplicates

## Setup

### Environment variables

```bash
GMAIL_CLIENT_ID=...
GMAIL_CLIENT_SECRET=...
GMAIL_REFRESH_TOKEN=...
HUBSPOT_API_KEY=...
```

To get Gmail credentials: <https://console.cloud.google.com/> → OAuth2 → scope `gmail.readonly`.  
To get HubSpot key: Settings → Integrations → Private Apps.

### Install dependencies

```bash
# No npm dependencies — uses Node.js built-in `https` module only
node --version   # requires Node 18+
```

### Run once

```bash
node gmail_hubspot_sync.js
```

### Schedule (every 30 min via Claude Code loop)

```
/loop 30m node gmail_hubspot_sync.js
```

## Output format

```
✅ CREATO       support@example.com                      ID: 818364964080
🔄 AGGIORNATO   info@company.it                          ID: 431403283697
⏭️  IGNORATO     hello@known.com                          ID: 812158245059
```

## Filters

Senders skipped automatically:
- Prefixes: `no-reply`, `noreply`, `notifications`, `mailer-daemon`, `bounces`, `auto-reply`
- Domains: `google.com`, `gmail.com`, `facebook.com`, `linkedin.com`, `twitter.com`

Customise the `CONFIG` object at the top of `gmail_hubspot_sync.js`.

## HubSpot fields populated

| Field | Source |
|---|---|
| `email` | Sender address |
| `firstname` | Name part before last word |
| `lastname` | Last word of display name |
| `company` | Domain → human-readable company name |
| `hs_analytics_source` | `OTHER` (Gmail inbound) |
| Note | "Fonte: Gmail. Tag: Inbound Gmail" |
