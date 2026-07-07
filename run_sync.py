"""
Entry point for the scheduled Gmail → HubSpot sync routine.

This script is designed to be executed by Claude Code scheduled routines.
It uses the MCP tool layer for Gmail and HubSpot, so it documents the
expected MCP calls rather than making HTTP calls directly.

Execution flow
--------------
1. Search Gmail inbox for emails newer than 24 h (or since last run)
2. Extract unique real-person senders, skipping automated addresses
3. For each sender, lookup HubSpot contact by email
4. Create contact if not found; update missing fields if found
5. Write processed thread IDs to state file to avoid re-processing
6. Print structured report for the notification layer
"""

# ── MCP tool call documentation ───────────────────────────────────────────────
# The following shows the exact MCP calls made in each step.
# Actual execution happens through the Claude Code MCP runtime.

GMAIL_SEARCH_QUERY = "in:inbox newer_than:1d -from:me -in:draft -in:sent"

HUBSPOT_CONTACT_PROPERTIES = [
    "email",
    "firstname",
    "lastname",
    "company",
    "hs_lead_source",
    "domain",
]

# Contact fields set when creating/updating from Gmail
CONTACT_DEFAULTS = {
    "hs_lead_source": "Gmail",
}

# ── sync logic (pure, testable) ───────────────────────────────────────────────
from gmail_hubspot_sync import (  # noqa: E402
    extract_contact,
    process_threads,
    build_hubspot_properties,
    determine_status,
    format_report,
    load_processed_ids,
    save_processed_ids,
    SyncResult,
)


def run(gmail_threads: list[dict], hubspot_lookup: dict[str, dict | None]) -> list[SyncResult]:
    """
    Pure sync logic — all I/O is injected.

    Parameters
    ----------
    gmail_threads:
        Raw thread objects returned by mcp__Gmail__search_threads.
    hubspot_lookup:
        Dict mapping email → existing HubSpot contact dict (or None if missing).
    """
    processed = load_processed_ids()
    contacts = process_threads(gmail_threads)
    results: list[SyncResult] = []

    for c in contacts:
        email = c["email"]
        existing = hubspot_lookup.get(email)
        ex_props = existing.get("properties", {}) if existing else {}
        ex_id = existing.get("id") if existing else None

        new_props = build_hubspot_properties(
            type("C", (), c)(),  # quick adapter
            ex_props,
        )

        status = determine_status(ex_id, new_props)
        results.append(
            SyncResult(
                email=email,
                hubspot_id=str(ex_id) if ex_id else None,
                status=status,
                detail=str(new_props) if new_props else "",
            )
        )

    # persist thread ids so next run skips already-seen threads
    new_ids = {msg["id"] for t in gmail_threads for msg in t.get("messages", [])}
    save_processed_ids(processed | new_ids)

    return results


if __name__ == "__main__":
    # Dry-run mode: print the expected MCP call sequence
    print("Gmail → HubSpot Sync — MCP call sequence")
    print()
    print("Step 1: mcp__Gmail__search_threads")
    print(f'  query="{GMAIL_SEARCH_QUERY}", pageSize=50')
    print()
    print("Step 2: For each unique non-automated sender email →")
    print("  mcp__HubSpot__search_crm_objects(objectType='contacts', filter by email IN [...])")
    print()
    print("Step 3a: New contact →")
    print("  mcp__HubSpot__manage_crm_objects(createRequest={...})")
    print()
    print("Step 3b: Existing contact with missing fields →")
    print("  mcp__HubSpot__manage_crm_objects(updateRequest={objectId, properties: {...}})")
    print()
    print("Step 4: Log results and send push notification.")
