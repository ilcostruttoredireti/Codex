"""
Gmail → HubSpot contact sync.

Polls Gmail for new inbound messages using the History API and upserts
the sender as a HubSpot contact, avoiding duplicates.

Usage:
    python main.py

Environment variables (see .env.example):
    HUBSPOT_ACCESS_TOKEN   – HubSpot private-app token (required)
    GMAIL_CREDENTIALS_FILE – path to OAuth2 credentials JSON (default: credentials.json)
    GMAIL_TOKEN_FILE       – path to stored OAuth2 token (default: token.json)
    POLL_INTERVAL_SECONDS  – polling cadence in seconds (default: 60)
    STATE_FILE             – path to persisted state JSON (default: state.json)
"""

import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from gmail_client import GmailClient, HistoryExpiredError
from hubspot_client import HubSpotClient
from processor import build_contact_properties, missing_fields, parse_sender

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("State file unreadable, starting fresh.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Single-message processing
# ---------------------------------------------------------------------------


def process_message(
    gmail: GmailClient,
    hubspot: HubSpotClient,
    message_id: str,
    own_email: str,
) -> dict:
    """
    Process one Gmail message and upsert the sender in HubSpot.

    Returns a result dict with keys: status, email, hubspot_id.
    """
    headers = gmail.get_message_headers(message_id)
    from_header = headers.get("From", "").strip()
    subject = headers.get("Subject", "(no subject)")

    if not from_header:
        return {"status": "Ignorato", "reason": "missing From header"}

    name, email_addr = parse_sender(from_header)

    if not email_addr or "@" not in email_addr:
        return {"status": "Ignorato", "reason": "invalid sender address", "email": from_header}

    # Skip messages sent by the account owner to themselves
    if email_addr == own_email.lower():
        return {"status": "Ignorato", "reason": "self-email", "email": email_addr}

    candidate = build_contact_properties(name, email_addr)
    existing = hubspot.find_contact_by_email(email_addr)

    if existing:
        contact_id = existing["id"]
        updates = missing_fields(existing.get("properties", {}), candidate)
        if updates:
            hubspot.update_contact(contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"
        hubspot.log_email_activity(contact_id, subject, from_header)
        return {"status": status, "email": email_addr, "hubspot_id": contact_id}
    else:
        contact = hubspot.create_contact(candidate)
        contact_id = contact["id"]
        hubspot.log_email_activity(contact_id, subject, from_header)
        return {"status": "Creato", "email": email_addr, "hubspot_id": contact_id}


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------


def main() -> None:
    access_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not access_token:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN is not set. Check your .env file.")

    gmail = GmailClient(
        credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
    )
    hubspot = HubSpotClient(access_token=access_token)

    own_email: str = gmail.get_profile()["emailAddress"].lower()
    log.info("Authenticated as: %s", own_email)

    state = load_state()

    # Bootstrap: capture current historyId without processing old messages
    if "history_id" not in state:
        state["history_id"] = gmail.get_profile()["historyId"]
        save_state(state)
        log.info("Initialized historyId: %s — watching for new messages.", state["history_id"])

    log.info("Polling every %ds. Press Ctrl+C to stop.", POLL_INTERVAL)

    while True:
        try:
            message_ids, new_history_id = gmail.get_new_messages(state["history_id"])

            for msg_id in message_ids:
                try:
                    result = process_message(gmail, hubspot, msg_id, own_email)
                    log.info(
                        "[%-10s]  %-40s  HubSpot ID: %s",
                        result["status"],
                        result.get("email", "—"),
                        result.get("hubspot_id", "N/A"),
                    )
                except Exception:
                    log.exception("Error processing message %s — skipping.", msg_id)

            state["history_id"] = new_history_id
            save_state(state)

        except HistoryExpiredError:
            log.warning("historyId expired — resetting to current position.")
            state["history_id"] = gmail.get_profile()["historyId"]
            save_state(state)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break

        except Exception:
            log.exception("Unexpected error during poll — retrying after %ds.", POLL_INTERVAL)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
