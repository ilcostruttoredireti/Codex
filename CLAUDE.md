# Gmail → HubSpot Contact Sync Routine

## Purpose
Monitor all incoming Gmail emails, extract sender contacts, and sync them to HubSpot — creating new contacts or updating existing ones, with no duplicates.

## How it works
1. Read `state/processed_threads.json` to get the list of already-processed Gmail thread IDs.
2. Search Gmail inbox for threads newer than 7 days (`in:inbox -from:me newer_than:7d`).
3. For each thread not yet in the processed list:
   - Extract the actual sender email and name from the `sender` field (or from the forwarded "Da:" header in the snippet if the sender is the internal relay `redazione@latestata.it`).
   - Skip automated/internal senders: `notification@*`, `noreply@*`, `no-reply@*`, `redazione@latestata.it`, `pubblica.latestata@gmail.com`, `cristian.mameli.editore@gmail.com`.
4. Deduplicate by email address across threads.
5. For each unique sender email, search HubSpot contacts with `email EQ <address>`.
6. **If not found** → create a new HubSpot contact with:
   - `email`, `firstname`, `lastname` (parsed from name if available), `company` (derived from domain), `hs_lead_source = "OTHER"`, `hs_analytics_source_data_2 = "Inbound Gmail"`
7. **If found** → update only missing fields (never overwrite existing values).
8. Append processed thread IDs to `state/processed_threads.json`.
9. Append a run summary row to `logs/sync_log.jsonl`.

## Output per email
Each processed thread produces one of:
- `CREATO` — new contact created in HubSpot
- `AGGIORNATO` — existing contact updated with missing fields
- `IGNORATO` — contact already complete, or sender skipped

## Files
- `state/processed_threads.json` — persisted list of processed Gmail thread IDs
- `logs/sync_log.jsonl` — append-only JSONL run log (one JSON object per processed sender)
