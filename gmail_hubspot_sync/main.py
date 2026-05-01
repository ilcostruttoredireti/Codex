"""
Gmail → HubSpot contact sync loop.

Polls the Gmail inbox continuously, extracts sender information from each new
message, then creates or updates the corresponding HubSpot contact.

Output per processed email
--------------------------
[CREATO   ] mario.rossi@azienda.it  →  HubSpot ID: 12345
[AGGIORNATO] info@example.com        →  HubSpot ID: 67890
[IGNORATO ] newsletter@acme.com      →  HubSpot ID: N/A
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from gmail_hubspot_sync.contact_parser import parse_from_header
from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE: str = os.getenv("STATE_FILE", "sync_state.json")
GOOGLE_CREDENTIALS: str = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials.json")
GOOGLE_TOKEN: str = os.getenv("GOOGLE_TOKEN_PATH", "token.json")
HUBSPOT_TOKEN: str = os.getenv("HUBSPOT_API_TOKEN", "")

STATUS_LABEL = {
    "created": "CREATO    ",
    "updated": "AGGIORNATO",
    "ignored": "IGNORATO  ",
}


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"last_history_id": None, "processed_ids": []}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------

def _process_message(
    gmail: GmailClient,
    hubspot: HubSpotClient,
    message_id: str,
    processed_ids: set[str],
) -> dict | None:
    """
    Process a single Gmail message stub.

    Returns a result dict or None if the message was already processed or
    could not be handled.
    """
    if message_id in processed_ids:
        return None

    headers = gmail.get_message_headers(message_id)
    from_header = headers.get("From", "")

    if not from_header:
        return None

    contact = parse_from_header(from_header)
    if contact is None:
        # no-reply, unparse-able, etc.
        return {"status": "ignored", "email": from_header, "hubspot_id": None}

    try:
        status, contact_id = hubspot.upsert_contact(contact)
    except Exception as exc:
        logger.error("HubSpot upsert failed for %s: %s", contact["email"], exc)
        return None

    if status in ("created", "updated"):
        subject = headers.get("Subject", "")
        hubspot.add_email_activity_note(contact_id, subject, contact["email"])

    return {"status": status, "email": contact["email"], "hubspot_id": contact_id}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_API_TOKEN environment variable is not set")

    gmail = GmailClient(credentials_path=GOOGLE_CREDENTIALS, token_path=GOOGLE_TOKEN)
    hubspot = HubSpotClient(api_token=HUBSPOT_TOKEN)

    state = _load_state()
    last_history_id: str | None = state["last_history_id"]
    processed_ids: set[str] = set(state["processed_ids"])

    logger.info("Gmail → HubSpot sync avviato  (polling ogni %ds)", POLL_INTERVAL)

    while True:
        try:
            messages, new_history_id = gmail.get_new_messages(last_history_id)

            for stub in messages:
                msg_id: str = stub if isinstance(stub, str) else stub["id"]
                result = _process_message(gmail, hubspot, msg_id, processed_ids)

                if result:
                    label = STATUS_LABEL.get(result["status"], result["status"].upper())
                    hub_id = result["hubspot_id"] or "N/A"
                    logger.info("[%s]  %-45s  →  HubSpot ID: %s", label, result["email"], hub_id)

                processed_ids.add(msg_id)

            # Cap set size to avoid unbounded growth
            if len(processed_ids) > 2000:
                processed_ids = set(list(processed_ids)[-2000:])

            last_history_id = new_history_id
            _save_state({"last_history_id": last_history_id, "processed_ids": list(processed_ids)})

        except KeyboardInterrupt:
            logger.info("Sync interrotto.")
            break
        except Exception as exc:
            logger.error("Errore nel ciclo principale: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
