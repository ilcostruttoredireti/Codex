#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail e sincronizza i mittenti come contatti HubSpot.
"""

import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from gmail_monitor import GmailMonitor
from hubspot_sync import HubSpotSync, SyncStatus
from state_manager import StateManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gmail_hubspot_sync")


def _load_config() -> dict:
    required = {"HUBSPOT_ACCESS_TOKEN"}
    config = {
        "hubspot_token": os.environ.get("HUBSPOT_ACCESS_TOKEN", ""),
        "gmail_credentials": os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        "gmail_token": os.environ.get("GMAIL_TOKEN_FILE", "token.json"),
        "poll_interval": int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        "state_file": os.environ.get("STATE_FILE", "sync_state.json"),
        "ignored_domains": set(
            d.strip()
            for d in os.environ.get("IGNORED_DOMAINS", "").split(",")
            if d.strip()
        ),
    }
    missing = {k for k in required if not os.environ.get(k)}
    if missing:
        logger.error("Variabili d'ambiente mancanti: %s", ", ".join(missing))
        sys.exit(1)
    return config


def _print_result_row(result):
    status_icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}.get(result.status, "?")
    contact_id = result.contact_id or "—"
    extra = f"  [{result.message}]" if result.message else ""
    print(f"  {status_icon} {result.status:<10}  {result.email:<40}  ID: {contact_id}{extra}")


def run():
    config = _load_config()

    gmail = GmailMonitor(
        credentials_file=config["gmail_credentials"],
        token_file=config["gmail_token"],
        ignored_domains=config["ignored_domains"],
    )
    hubspot = HubSpotSync(access_token=config["hubspot_token"])
    state = StateManager(state_file=config["state_file"])

    logger.info("Avvio sincronizzazione Gmail → HubSpot (polling ogni %ds)", config["poll_interval"])

    while True:
        cycle_start = datetime.now()
        logger.info("--- Ciclo %s ---", cycle_start.strftime("%Y-%m-%d %H:%M:%S"))

        try:
            messages = gmail.get_new_messages(since_iso=state.get_last_check())
            logger.info("Trovate %d email nel periodo", len(messages))

            created = updated = ignored = 0

            for msg in messages:
                msg_id = msg["id"]

                if state.is_processed(msg_id):
                    continue

                sender = gmail.get_message_sender(msg_id)
                if not sender:
                    state.mark_processed(msg_id)
                    continue

                result = hubspot.sync_contact(sender)
                state.mark_processed(msg_id)
                _print_result_row(result)

                if result.status == SyncStatus.CREATED:
                    created += 1
                elif result.status == SyncStatus.UPDATED:
                    updated += 1
                else:
                    ignored += 1

            logger.info("Riepilogo ciclo → Creati: %d | Aggiornati: %d | Ignorati: %d", created, updated, ignored)
            state.update_last_check()

        except KeyboardInterrupt:
            logger.info("Interruzione richiesta. Uscita.")
            break
        except Exception as exc:
            logger.exception("Errore durante il ciclo di sincronizzazione: %s", exc)

        time.sleep(config["poll_interval"])


if __name__ == "__main__":
    run()
