#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Polls Gmail every POLL_INTERVAL_SECONDS for new inbox messages,
extracts sender info, and creates/updates contacts in HubSpot.
"""

import logging
import time

from config import Config
from gmail_client import SenderInfo, build_gmail_service, fetch_new_messages
from hubspot_client import SyncStatus, sync_contact
from state import load_history_id, save_history_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def process_sender(sender: SenderInfo) -> None:
    result = sync_contact(sender)

    icon = {"Creato": "✚", "Aggiornato": "↺", "Ignorato": "–"}.get(result.status.value, "?")
    log.info(
        "%s %-10s  email=%-40s  hubspot_id=%s  gmail_msg=%s",
        icon,
        result.status.value,
        result.email,
        result.contact_id,
        result.message_id,
    )


def run() -> None:
    Config.validate()

    log.info("Building Gmail service…")
    service = build_gmail_service()

    history_id = load_history_id()
    if history_id is None:
        log.info("First run — recording current historyId, no messages processed yet.")
        _, history_id = fetch_new_messages(service, None)
        save_history_id(history_id)
        log.info("historyId saved: %s. Waiting for next poll…", history_id)

    log.info(
        "Sync loop started. Poll interval: %ds. HubSpot token: %s…",
        Config.POLL_INTERVAL_SECONDS,
        Config.HUBSPOT_ACCESS_TOKEN[:8],
    )

    while True:
        try:
            senders, new_history_id = fetch_new_messages(service, history_id)

            if senders:
                log.info("Found %d new inbox message(s).", len(senders))
                for sender in senders:
                    try:
                        process_sender(sender)
                    except Exception as exc:
                        log.error("Failed to sync %s: %s", sender.email, exc)
            else:
                log.debug("No new messages.")

            if new_history_id != history_id:
                history_id = new_history_id
                save_history_id(history_id)

        except Exception as exc:
            log.error("Poll error: %s", exc, exc_info=True)

        time.sleep(Config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
