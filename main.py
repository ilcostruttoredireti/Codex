#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync
Polls Gmail inbox for new senders and creates/updates HubSpot contacts.
"""

import os
import sys
import time
import logging

from dotenv import load_dotenv

from gmail_monitor import GmailMonitor
from contact_sync import ContactSync

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
CREATE_NOTES = os.getenv("CREATE_NOTES", "true").lower() == "true"

STATUS_WIDTH = 10
EMAIL_WIDTH = 40


def print_result(result: dict):
    status = result["status"]
    email = result["email"]
    contact_id = result.get("id") or "N/A"

    color = {
        "Creato": "\033[32m",       # green
        "Aggiornato": "\033[33m",   # yellow
        "Ignorato": "\033[90m",     # dark grey
        "Errore": "\033[31m",       # red
    }.get(status, "")
    reset = "\033[0m"

    print(
        f"{color}[{status:<{STATUS_WIDTH}}]{reset}  "
        f"{email:<{EMAIL_WIDTH}}  ID: {contact_id}"
    )


def main():
    if not HUBSPOT_TOKEN:
        sys.exit("ERROR: HUBSPOT_TOKEN is not set. Add it to your .env file.")

    try:
        gmail = GmailMonitor(credentials_file=CREDENTIALS_FILE)
    except FileNotFoundError as exc:
        sys.exit(f"ERROR: {exc}")

    sync = ContactSync(HUBSPOT_TOKEN, create_notes=CREATE_NOTES)

    logger.info(
        "Gmail → HubSpot sync avviato  |  account: %s  |  polling ogni %ds",
        gmail.user_email,
        POLL_INTERVAL,
    )
    print(f"\n{'STATUS':<{STATUS_WIDTH + 2}}  {'EMAIL':<{EMAIL_WIDTH}}  HUBSPOT ID")
    print("-" * 75)

    while True:
        try:
            senders = gmail.fetch_new_emails()

            if senders:
                logger.info("Trovate %d nuove email da processare.", len(senders))
                for sender in senders:
                    result = sync.sync(sender)
                    print_result(result)

        except KeyboardInterrupt:
            print("\nInterrotto dall'utente.")
            break
        except Exception as exc:
            logger.error("Errore inatteso: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
