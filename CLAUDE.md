# Gmail → HubSpot Contact Sync

## What this does

Monitors all incoming Gmail messages, extracts the sender's contact data, and
syncs it into HubSpot — creating new contacts or updating existing ones.

## How it works

1. **Gmail scan** – `mcp__Gmail__search_threads` fetches all inbox threads.
2. **Filter automated senders** – addresses matching no-reply/bot patterns are
   skipped (see `SKIP_LOCAL_PARTS` and `SKIP_DOMAINS` in `gmail_hubspot_sync.py`).
3. **Dedup** – `mcp__HubSpot__search_crm_objects` checks whether each unique
   sender email already exists in HubSpot (email is the unique key).
4. **Create or update** – new contacts are created via
   `mcp__HubSpot__manage_crm_objects`; existing ones are patched only for
   missing/empty fields.
5. **Run log** – each execution appends a JSONL record to `logs/sync_run.jsonl`.

## Fields set in HubSpot

| HubSpot property | Source |
|------------------|--------|
| `email`          | Sender address |
| `firstname`      | Local-part parse / display name |
| `lastname`       | Local-part parse / display name |
| `company`        | Derived from sender domain |
| `hs_lead_source` | Hard-coded `"Gmail"` |

## Output statuses

| Status    | Meaning |
|-----------|---------|
| Creato    | New contact created in HubSpot |
| Aggiornato | Existing contact updated with missing fields |
| Ignorato  | Contact already complete — no action needed |
| Saltato   | Automated / no-reply address — skipped |

## Run log

`logs/sync_run.jsonl` — one JSON record per run, appended each execution.

## Files

- `gmail_hubspot_sync.py` — core business logic (extraction, dedup, field-merge)
- `logs/sync_run.jsonl`   — execution history
