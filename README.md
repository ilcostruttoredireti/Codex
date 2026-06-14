# Gmail → HubSpot Contact Sync

Scheduled routine that monitors Gmail inbox, extracts unique senders, and syncs them as contacts in HubSpot — avoiding duplicates and filling in missing fields.

## What it does

For every email in the Gmail inbox (last 24 h by default):

1. **Extracts** sender email, name, and company domain.
2. **Skips** automated senders (noreply, notifications, own account).
3. **Checks HubSpot** by email (unique key).
4. **Creates** new contact if not found — with `hs_lead_source = Gmail`.
5. **Updates** existing contact to fill any missing fields (firstname, lastname, company, source).
6. Returns a per-contact status: **Creato / Aggiornato / Ignorato**.

## Execution modes

| Mode | How | Auth |
|------|-----|------|
| **Claude Agent SDK** (primary) | Runs as a scheduled routine using Gmail + HubSpot MCP tools | OAuth via connected apps |
| **Standalone** | `python gmail_hubspot_sync.py` | `GMAIL_CREDENTIALS` + `HUBSPOT_API_KEY` env vars |

## Fields populated in HubSpot

| HubSpot Field | Source |
|---------------|--------|
| `email` | Sender address |
| `firstname` | Display name (first word) or local part of email |
| `lastname` | Display name (remaining words, if any) |
| `company` | Derived from email domain |
| `hs_lead_source` | `"Gmail"` |

## Deduplication

Email address is the unique key. The routine never creates two contacts with the same email.

## Files

- `gmail_hubspot_sync.py` — core logic + standalone runner
