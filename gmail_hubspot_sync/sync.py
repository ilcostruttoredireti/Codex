"""Core sync logic: Gmail → HubSpot."""
import json
import os
from dataclasses import asdict, dataclass

import config
from gmail_client import SenderInfo, fetch_new_senders
from hubspot_client import ContactResult, upsert_contact


def _load_state() -> dict:
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as f:
            return json.load(f)
    return {"processed": []}


def _save_state(state: dict) -> None:
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


@dataclass
class SyncRecord:
    status: str
    email: str
    contact_id: str | None
    subject: str
    reason: str = ""


def run_sync() -> list[SyncRecord]:
    """Fetch new emails, upsert contacts, persist state. Returns one record per email."""
    state = _load_state()
    processed: set[str] = set(state.get("processed", []))

    new_senders: list[SenderInfo] = fetch_new_senders(processed)

    # De-duplicate by email within the same batch
    seen_emails: set[str] = set()
    unique_senders: list[SenderInfo] = []
    for s in new_senders:
        if s.email not in seen_emails:
            seen_emails.add(s.email)
            unique_senders.append(s)

    records: list[SyncRecord] = []
    for sender in unique_senders:
        result: ContactResult = upsert_contact(
            email=sender.email,
            name=sender.name,
            domain=sender.domain,
            subject=sender.subject,
        )
        records.append(
            SyncRecord(
                status=result.status,
                email=result.email,
                contact_id=result.contact_id,
                subject=sender.subject,
                reason=result.reason,
            )
        )

    # Mark all original message IDs as processed (even skipped ones)
    for s in new_senders:
        processed.add(s.message_id)

    state["processed"] = list(processed)
    _save_state(state)
    return records
