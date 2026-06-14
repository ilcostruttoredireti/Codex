# Gmail → HubSpot Contact Sync

Scheduled routine that monitors Gmail inbox and syncs sender contacts to HubSpot CRM.

## What it does

For every new email in the inbox (last 7 days, unread):

1. Extracts the **direct sender** and, for forwarded emails, the **original sender** embedded in the body (`Da "Name" addr@domain`).
2. Skips automated/noreply addresses (`noreply`, `no-reply`, `analytics-noreply`, etc.).
3. Looks up the contact in HubSpot by email (unique key).
   - **Not found** → creates a new contact with all available fields + `lead_source = "Gmail"`.
   - **Found** → patches only the fields that are currently empty (firstname, lastname, company, lead_source).
4. Prints a per-email report: `Creato / Aggiornato / Ignorato`.

## Output format

```
Stato        Email                                              ID HubSpot
-----------------------------------------------------------------------
Ignorato     redazione@latestata.it                             395702512840
Ignorato     carola.assumma@carolaassummacomunicazione.com      744197629154
Ignorato     runningsicily2016@gmail.com                        759119667435
Ignorato     analytics-noreply@google.com                       -
```

## Run log — 2026-06-14

| Stato | Email | ID HubSpot |
|-------|-------|-----------|
| Ignorato | redazione@latestata.it | 395702512840 |
| Ignorato | carola.assumma@carolaassummacomunicazione.com | 744197629154 |
| Ignorato | runningsicily2016@gmail.com | 759119667435 |
| Ignorato (noreply) | analytics-noreply@google.com | — |

All 3 real contacts were already present in HubSpot. No new records created.

## Module: `gmail_hubspot_sync.py`

Core functions:

| Function | Description |
|----------|-------------|
| `parse_sender(header)` | Parses a `From:` header into a `ContactData` |
| `extract_forwarded_sender(body)` | Extracts original sender from Italian-style forwarded email body |
| `sync_contact(cd, ...)` | Creates or updates a HubSpot contact; returns `SyncResult` |
| `process_threads(threads, ...)` | Processes a list of Gmail threads end-to-end |
| `print_report(results)` | Prints tabular summary |
