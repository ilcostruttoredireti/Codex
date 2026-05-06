"""
Gmail → HubSpot contact sync.

Continuously polls Gmail for new inbound emails and upserts senders as
HubSpot contacts.

Usage:
    python main.py

Required env vars (or .env file):
    HUBSPOT_ACCESS_TOKEN
    GMAIL_CREDENTIALS_FILE  (default: credentials.json)
    GMAIL_TOKEN_FILE        (default: token.json)

Optional:
    POLL_INTERVAL_SECONDS   (default: 60)
    GMAIL_PROCESSED_LABEL   (default: HubSpot-Synced)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient, SyncStatus

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Domains to skip (own company / bots / noreply)
SKIP_DOMAINS: set[str] = {"noreply.com", "notifications.github.com"}
SKIP_PREFIXES: tuple[str, ...] = ("noreply@", "no-reply@", "mailer-daemon@", "postmaster@")


def _should_skip(email: str) -> bool:
    email = email.lower()
    if any(email.startswith(p) for p in SKIP_PREFIXES):
        return True
    domain = email.split("@")[1] if "@" in email else ""
    return domain in SKIP_DOMAINS


def _print_result(status: SyncStatus, email: str, contact_id: str) -> None:
    icons = {SyncStatus.CREATED: "✅", SyncStatus.UPDATED: "🔄", SyncStatus.IGNORED: "⏭️"}
    icon = icons.get(status, "")
    log.info("%s  Stato: %-10s  Email: %-40s  ID HubSpot: %s", icon, status.value, email, contact_id or "—")


def run() -> None:
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    if not hubspot_token:
        log.error("HUBSPOT_ACCESS_TOKEN non impostato. Controlla il file .env.")
        sys.exit(1)

    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    processed_label = os.getenv("GMAIL_PROCESSED_LABEL", "HubSpot-Synced")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

    if not Path(credentials_file).exists():
        log.error(
            "File credenziali Gmail '%s' non trovato.\n"
            "Scarica le credenziali OAuth2 dalla Google Cloud Console e salvale come '%s'.",
            credentials_file,
            credentials_file,
        )
        sys.exit(1)

    log.info("Inizializzazione client Gmail …")
    gmail = GmailClient(credentials_file, token_file, processed_label)

    log.info("Inizializzazione client HubSpot …")
    hubspot_client = HubSpotClient(hubspot_token)

    log.info("Avvio monitoraggio Gmail (polling ogni %ds) …", poll_interval)

    history_id: str | None = None

    while True:
        try:
            messages, history_id = gmail.fetch_new_messages(after_history_id=history_id)

            if messages:
                log.info("--- %d nuovi messaggi trovati ---", len(messages))
            else:
                log.debug("Nessun nuovo messaggio.")

            for msg in messages:
                sender = msg["sender"]
                email_addr = sender["email"]

                if _should_skip(email_addr):
                    log.info("⏭️  Saltato (bot/noreply): %s", email_addr)
                    gmail.mark_processed(msg["msg_id"])
                    continue

                result = hubspot_client.sync_contact(
                    email=email_addr,
                    first_name=sender["first_name"],
                    last_name=sender["last_name"],
                    domain=sender["domain"],
                    subject=msg["subject"],
                    date=msg["date"],
                )
                _print_result(result.status, result.email, result.contact_id)
                gmail.mark_processed(msg["msg_id"])

        except KeyboardInterrupt:
            log.info("Interruzione manuale — arresto.")
            break
        except Exception as exc:
            log.error("Errore durante il polling: %s", exc, exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    run()
