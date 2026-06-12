"""Core sync logic: Gmail → HubSpot contact upsert."""

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from . import gmail_client, hubspot_client
from .contact_parser import parse_sender

log = logging.getLogger(__name__)

Status = Literal["CREATO", "AGGIORNATO", "IGNORATO"]

GMAIL_LABEL = "Inbound Gmail"
SOURCE_LABEL = "Gmail"


@dataclass
class SyncResult:
    status: Status
    email: str
    contact_id: str | None
    msg_id: str


@dataclass
class SyncState:
    processed_ids: set[str] = field(default_factory=set)
    last_timestamp: int = 0          # Unix seconds — used for Gmail `after:` filter
    gmail_label_id: str | None = None


def _process_message(gmail_svc, msg_id: str, state: SyncState) -> SyncResult:
    if msg_id in state.processed_ids:
        return SyncResult("IGNORATO", "", None, msg_id)

    headers = gmail_client.get_message_headers(gmail_svc, msg_id)
    sender = parse_sender(headers["from"])

    if not sender or not sender.get("email"):
        state.processed_ids.add(msg_id)
        return SyncResult("IGNORATO", headers.get("from", ""), None, msg_id)

    addr = sender["email"]
    props = hubspot_client.build_contact_props(sender, source_label=SOURCE_LABEL)

    existing = hubspot_client.find_contact_by_email(addr)

    if existing:
        contact_id = existing["id"]
        updates = hubspot_client.missing_props(existing, props)
        if updates:
            hubspot_client.update_contact(contact_id, updates)

        note_body = (
            f"📧 Email in entrata da Gmail\n"
            f"Da: {sender['full_name']} <{addr}>\n"
            f"Oggetto: {headers.get('subject', '—')}\n"
            f"Data: {headers.get('date', '—')}"
        )
        hubspot_client.create_note(contact_id, note_body)
        status: Status = "AGGIORNATO"

    else:
        result = hubspot_client.create_contact(props)
        contact_id = result["id"]

        note_body = (
            f"📧 Primo contatto via Gmail — contatto creato automaticamente\n"
            f"Da: {sender['full_name']} <{addr}>\n"
            f"Dominio: {sender.get('domain', '—')}\n"
            f"Oggetto: {headers.get('subject', '—')}\n"
            f"Data: {headers.get('date', '—')}\n"
            f"Fonte: {SOURCE_LABEL}"
        )
        hubspot_client.create_note(contact_id, note_body)
        status = "CREATO"

    # Tag the Gmail thread
    if state.gmail_label_id and headers.get("thread_id"):
        try:
            gmail_client.add_label_to_thread(gmail_svc, headers["thread_id"], state.gmail_label_id)
        except Exception as exc:
            log.warning("Impossibile aggiungere label Gmail: %s", exc)

    state.processed_ids.add(msg_id)
    return SyncResult(status, addr, contact_id, msg_id)


def run_cycle(gmail_svc, state: SyncState) -> list[SyncResult]:
    """Fetch new inbox messages and sync each sender to HubSpot."""
    messages = gmail_client.list_inbox_messages(
        gmail_svc,
        after_timestamp=state.last_timestamp or None,
    )

    if not messages:
        log.debug("Nessun nuovo messaggio.")
        return []

    # Ensure Gmail label exists
    if state.gmail_label_id is None:
        try:
            state.gmail_label_id = gmail_client.get_or_create_label(gmail_svc, GMAIL_LABEL)
        except Exception as exc:
            log.warning("Impossibile ottenere/creare label '%s': %s", GMAIL_LABEL, exc)

    log.info("Messaggi da processare: %d", len(messages))
    results: list[SyncResult] = []

    for msg in messages:
        try:
            r = _process_message(gmail_svc, msg["id"], state)
            results.append(r)
            _log_result(r)
        except Exception as exc:
            log.error("Errore su msg %s: %s", msg["id"], exc)

    # Advance the timestamp so next run only fetches newer messages
    now_ts = int(time.time())
    if now_ts > state.last_timestamp:
        state.last_timestamp = now_ts

    # Bound the in-memory set to avoid unbounded growth over long runs
    if len(state.processed_ids) > 20_000:
        # Keep the most recently added (approximation — sets are unordered, so just trim)
        state.processed_ids = set(list(state.processed_ids)[-10_000:])

    return results


def _log_result(r: SyncResult) -> None:
    cid = r.contact_id or "—"
    addr = r.email or "—"
    log.info("%-12s | %-40s | ID: %s", r.status, addr, cid)
