"""
Core Gmail → HubSpot synchronisation logic.

Flow
----
1. On first run, fetch the most recent inbox messages and seed the Gmail
   history ID for subsequent incremental polls.
2. On every subsequent run, query the Gmail History API for messages added
   to INBOX since the last known historyId.
3. For each new message, extract the sender, then create or update the
   corresponding HubSpot contact.
4. Optionally log an inbound-email engagement on the contact timeline.
"""

import json
import os
from datetime import datetime, timezone

from config import AUTOMATED_PREFIXES, INITIAL_FETCH_LIMIT, PROCESSED_IDS_FILE
from contact_processor import extract_sender_info
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

HUBSPOT_SOURCE = "Gmail"


# ---------------------------------------------------------------------------
# Persistent state (processed message IDs + last historyId)
# ---------------------------------------------------------------------------

def _load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"processed_ids": [], "last_history_id": None}


def _save_state(path: str, state: dict) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Single-message processing
# ---------------------------------------------------------------------------

def _process_message(
    gmail: GmailClient,
    hs: HubSpotClient,
    message_id: str,
) -> dict:
    """
    Fetch one Gmail message, extract the sender, and sync to HubSpot.

    Returns a result dict with keys: status, email, hubspot_id.
    """
    details = gmail.get_message_headers(message_id)
    from_header = details.get("from", "")
    subject = details.get("subject", "")
    internal_date = details.get("internal_date", "")

    sender = extract_sender_info(from_header)
    if not sender:
        return {"status": "Ignorato", "email": from_header, "reason": "parse error"}

    email = sender["email"]

    # Skip automated senders
    local_part = email.split("@")[0]
    if any(local_part.startswith(p) for p in AUTOMATED_PREFIXES):
        return {"status": "Ignorato", "email": email, "reason": "automated sender"}

    existing = hs.find_contact_by_email(email)

    if existing:
        props = existing.properties
        updates = {}
        if not props.get("firstname") and sender["first_name"]:
            updates["firstname"] = sender["first_name"]
        if not props.get("lastname") and sender["last_name"]:
            updates["lastname"] = sender["last_name"]
        if not props.get("company") and sender["company"]:
            updates["company"] = sender["company"]

        contact_id = existing.id
        if updates:
            hs.update_contact(contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        new_props: dict = {
            "email": email,
            "hs_lead_source": HUBSPOT_SOURCE,
        }
        if sender["first_name"]:
            new_props["firstname"] = sender["first_name"]
        if sender["last_name"]:
            new_props["lastname"] = sender["last_name"]
        if sender["company"]:
            new_props["company"] = sender["company"]

        created = hs.create_contact(new_props)
        contact_id = created.id
        status = "Creato"

    # Log timeline engagement (non-fatal)
    received_at = (
        GmailClient.internal_date_to_iso(internal_date)
        if internal_date
        else datetime.now(tz=timezone.utc).isoformat()
    )
    hs.log_inbound_email(contact_id, subject, received_at)

    return {"status": status, "email": email, "hubspot_id": contact_id}


# ---------------------------------------------------------------------------
# Batch sync (one polling cycle)
# ---------------------------------------------------------------------------

def run_cycle(
    gmail: GmailClient,
    hs: HubSpotClient,
    state: dict,
) -> tuple[dict, list[dict]]:
    """
    Discover new inbox messages and process them.
    Returns the updated state and a list of per-message result dicts.
    """
    last_history_id = state.get("last_history_id")
    processed_ids: set = set(state.get("processed_ids", []))
    new_ids: list[str] = []

    if last_history_id:
        try:
            histories, new_history_id = gmail.list_history(last_history_id)
            for h in histories:
                for added in h.get("messagesAdded", []):
                    msg = added["message"]
                    if "INBOX" in msg.get("labelIds", []) and msg["id"] not in processed_ids:
                        new_ids.append(msg["id"])
            if new_history_id:
                state["last_history_id"] = new_history_id
        except RuntimeError:
            # historyId expired — fall back to full fetch and reseed
            last_history_id = None

    if not last_history_id:
        messages, _ = gmail.list_inbox_messages(max_results=INITIAL_FETCH_LIMIT)
        new_ids = [m["id"] for m in messages if m["id"] not in processed_ids]
        profile = gmail.get_profile()
        state["last_history_id"] = profile.get("historyId")

    results: list[dict] = []
    for msg_id in new_ids:
        try:
            result = _process_message(gmail, hs, msg_id)
        except Exception as exc:
            result = {"status": "Errore", "email": "?", "hubspot_id": None, "reason": str(exc)}
        result["message_id"] = msg_id
        results.append(result)
        processed_ids.add(msg_id)

    # Keep only the last 10 000 IDs to bound file size
    state["processed_ids"] = list(processed_ids)[-10_000:]
    return state, results
