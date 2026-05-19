import logging
import time

from . import config, state
from .gmail_client import SenderInfo, fetch_new_messages, get_current_history_id, get_service
from .hubspot_client import SyncResult, sync_contact

logger = logging.getLogger(__name__)


def _process_sender(sender: SenderInfo) -> SyncResult:
    return sync_contact(
        email=sender.email,
        first_name=sender.first_name,
        last_name=sender.last_name,
        domain=sender.domain,
        subject=sender.subject,
    )


def _log_result(result: SyncResult, sender: SenderInfo) -> None:
    status_map = {
        "created": "CREATO   ",
        "updated": "AGGIORNATO",
        "skipped": "IGNORATO ",
    }
    label = status_map.get(result.status, result.status.upper())
    extra = f" ({result.reason})" if result.reason else ""
    logger.info(
        "%-10s | email: %-40s | hubspot_id: %s%s",
        label,
        result.email,
        result.contact_id or "N/A",
        extra,
    )


def run_once() -> list[SyncResult]:
    """Single poll cycle. Returns list of SyncResult."""
    service = get_service()
    history_id = state.get_history_id()

    if history_id is None:
        # First run: record current history position and process recent inbox
        current_hid = get_current_history_id(service)
        senders, _ = fetch_new_messages(service, None)
        state.set_history_id(current_hid)
    else:
        senders, new_history_id = fetch_new_messages(service, history_id)
        state.set_history_id(new_history_id)

    results: list[SyncResult] = []
    for sender in senders:
        if state.is_processed(sender.message_id):
            continue
        result = _process_sender(sender)
        state.mark_processed(sender.message_id)
        _log_result(result, sender)
        results.append(result)

    if not results:
        logger.debug("Nessuna nuova email da processare.")

    return results


def run_loop() -> None:
    """Continuous polling loop."""
    logger.info("Avvio monitoraggio Gmail → HubSpot (intervallo: %ds)", config.POLL_INTERVAL_SECONDS)
    while True:
        try:
            run_once()
        except Exception as exc:
            logger.error("Errore durante il ciclo di sync: %s", exc, exc_info=True)
        time.sleep(config.POLL_INTERVAL_SECONDS)
