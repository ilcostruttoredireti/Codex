"""Core sync loop: Gmail → HubSpot."""
import json
import logging
import time
from pathlib import Path

from .config import (
    CONTACT_TAG,
    IGNORED_DOMAINS,
    IGNORED_EMAIL_PREFIXES,
    POLL_INTERVAL_SECONDS,
    PROCESSED_IDS_FILE,
)
from .gmail_client import SenderInfo, build_service, fetch_unread_inbox
from .hubspot_client import SyncResult, SyncStatus, upsert_contact

log = logging.getLogger(__name__)


def _load_processed_ids() -> set[str]:
    p = Path(PROCESSED_IDS_FILE)
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()


def _save_processed_ids(ids: set[str]) -> None:
    Path(PROCESSED_IDS_FILE).write_text(json.dumps(sorted(ids)))


def _should_skip(sender: SenderInfo) -> str | None:
    """Return a skip reason string, or None to proceed."""
    local = sender.email.split("@")[0]
    if local in IGNORED_EMAIL_PREFIXES:
        return f"prefisso ignorato ({local})"
    if sender.domain in IGNORED_DOMAINS:
        return f"dominio ignorato ({sender.domain})"
    return None


def process_batch(service, processed_ids: set[str]) -> list[dict]:
    """Fetch unread messages, sync new senders to HubSpot. Returns log rows."""
    senders = fetch_unread_inbox(service)
    rows = []

    seen_emails: set[str] = set()  # deduplicate within this batch

    for sender in senders:
        if sender.message_id in processed_ids:
            continue

        processed_ids.add(sender.message_id)

        skip_reason = _should_skip(sender)
        if skip_reason:
            rows.append({
                "stato": SyncStatus.IGNORED,
                "email": sender.email,
                "id_hubspot": None,
                "nota": skip_reason,
            })
            continue

        if sender.email in seen_emails:
            rows.append({
                "stato": SyncStatus.IGNORED,
                "email": sender.email,
                "id_hubspot": None,
                "nota": "duplicato nel batch",
            })
            continue
        seen_emails.add(sender.email)

        result: SyncResult = upsert_contact(sender)
        rows.append({
            "stato": result.status,
            "email": result.email,
            "id_hubspot": result.contact_id,
            "nota": result.note,
        })
        log.info("[%s] %s — HubSpot ID: %s %s",
                 result.status.value, result.email, result.contact_id, result.note)

    return rows


def run_once() -> list[dict]:
    """Single pass: useful for testing or one-shot invocation."""
    service = build_service()
    processed_ids = _load_processed_ids()
    rows = process_batch(service, processed_ids)
    _save_processed_ids(processed_ids)
    return rows


def run_continuous() -> None:
    """Poll Gmail every POLL_INTERVAL_SECONDS forever."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("Avvio sync Gmail → HubSpot (intervallo: %ds)", POLL_INTERVAL_SECONDS)
    service = build_service()
    processed_ids = _load_processed_ids()

    while True:
        try:
            rows = process_batch(service, processed_ids)
            _save_processed_ids(processed_ids)
            if rows:
                log.info("Batch completato: %d email processate", len(rows))
        except Exception as exc:
            log.error("Errore nel batch: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL_SECONDS)
