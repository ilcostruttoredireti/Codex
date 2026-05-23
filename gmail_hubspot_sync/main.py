from __future__ import annotations
import logging
import sys
import time

from .config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    HUBSPOT_ACCESS_TOKEN,
    LOG_LEVEL,
    MAX_PROCESSED_IDS_CACHE,
    POLL_INTERVAL_SECONDS,
    STATE_FILE,
)
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state import SyncState
from .sync import ContactSyncer, SyncStatus


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    if not HUBSPOT_ACCESS_TOKEN:
        logger.error("HUBSPOT_ACCESS_TOKEN non impostato. Uscita.")
        sys.exit(1)

    gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)
    hubspot = HubSpotClient(HUBSPOT_ACCESS_TOKEN)
    state = SyncState(STATE_FILE)
    syncer = ContactSyncer(gmail, hubspot)

    # First run: capture current historyId as baseline (do not process old messages)
    if not state.history_id:
        state.history_id = gmail.get_current_history_id()
        state.save()
        logger.info("Prima esecuzione — historyId iniziale: %s", state.history_id)

    logger.info(
        "Monitoraggio Gmail avviato. Polling ogni %ds. historyId: %s",
        POLL_INTERVAL_SECONDS,
        state.history_id,
    )

    while True:
        try:
            _poll_once(syncer, gmail, state, logger)
        except KeyboardInterrupt:
            logger.info("Interruzione manuale. Uscita.")
            break
        except Exception as exc:
            logger.error("Errore nel ciclo di polling: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL_SECONDS)


def _poll_once(
    syncer: ContactSyncer,
    gmail: GmailClient,
    state: SyncState,
    logger: logging.Logger,
) -> None:
    message_ids, new_history_id = gmail.get_new_inbox_message_ids(state.history_id)

    if not message_ids:
        logger.debug("Nessun nuovo messaggio.")
        state.history_id = new_history_id
        state.save()
        return

    logger.info("Trovati %d nuovo/i messaggio/i", len(message_ids))

    counts = {SyncStatus.CREATED: 0, SyncStatus.UPDATED: 0, SyncStatus.SKIPPED: 0}

    for msg_id in message_ids:
        if state.is_processed(msg_id):
            logger.debug("Messaggio %s già processato, ignorato", msg_id)
            continue

        result = syncer.process_message(msg_id)
        state.mark_processed(msg_id, MAX_PROCESSED_IDS_CACHE)
        counts[result.status] += 1

        contact_info = f" → HubSpot ID: {result.contact_id}" if result.contact_id else ""
        skip_reason = (
            f" ({result.reason})"
            if result.reason and result.status == SyncStatus.SKIPPED
            else ""
        )
        logger.info(
            "[%s] %s%s%s",
            result.status.value,
            result.email or "(nessuna email)",
            contact_info,
            skip_reason,
        )

    state.history_id = new_history_id
    state.save()

    logger.info(
        "Ciclo completato — Creati: %d | Aggiornati: %d | Ignorati: %d",
        counts[SyncStatus.CREATED],
        counts[SyncStatus.UPDATED],
        counts[SyncStatus.SKIPPED],
    )
