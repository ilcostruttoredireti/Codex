# Gmail → HubSpot Contact Sync — Automation

## What this does

This repository hosts a scheduled Claude Code automation that:

1. Scans incoming Gmail inbox emails (last 30 days)
2. Extracts unique sender information (email, name, domain/company)
3. Looks up each sender in HubSpot CRM
4. Creates new contacts or updates existing ones — never duplicates
5. Sets `leadsource = "OTHER"` + `hs_lead_source_data_1 = "Gmail"` to tag origin

## Scheduled execution

The task runs as a Claude Code background session with Gmail MCP and HubSpot MCP
servers connected. Claude executes the sync logic via MCP tool calls:

| Step | Gmail tool | HubSpot tool |
|------|-----------|-------------|
| Fetch inbox emails | `mcp__Gmail__search_threads` | — |
| Look up contact | — | `mcp__HubSpot__search_crm_objects` |
| Create contact | — | `mcp__HubSpot__manage_crm_objects` (createRequest) |
| Update contact | — | `mcp__HubSpot__manage_crm_objects` (updateRequest) |

## Sync rules

- **Deduplication key**: email address (exact match)
- **Automated senders** are skipped (no-reply, noreply, notifications, etc.) — full
  list in `gmail_hubspot_sync.py :: AUTOMATED_PREFIXES`
- **Existing contacts**: only missing fields are filled in — existing data is never
  overwritten
- **New contacts** get:
  - `email` — from sender
  - `firstname` / `lastname` — inferred from email prefix
  - `company` — inferred from email domain
  - `leadsource = "OTHER"` with `hs_lead_source_data_1 = "Gmail"`

## Output per email processed

| Field | Values |
|-------|--------|
| Stato | `Creato` / `Aggiornato` / `Ignorato` |
| Email contatto | sender email |
| ID contatto HubSpot | numeric contact ID |

## Files

| File | Purpose |
|------|---------|
| `gmail_hubspot_sync.py` | Core parsing and sync logic (pure Python, no MCP deps) |
| `CLAUDE.md` | This file — automation documentation |

## Current HubSpot state (as of 2026-07-12)

- **643 contacts** synced from Gmail
- All inbox senders from the last 30 days are captured
- Latest contact created: `confirm@mailchimp.com` on 2026-07-11
