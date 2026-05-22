#!/usr/bin/env python3
"""Gmail → HubSpot Contact Sync

Monitors incoming Gmail and syncs each unique sender as a HubSpot contact.
Run once to initialise state, then loops every POLL_INTERVAL_SECONDS.

Usage:
    python gmail_hubspot_sync.py

Requires:
    - credentials.json  (Google OAuth2 desktop app credentials)
    - .env              (see .env.example)
"""

import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from contact_extractor import extract_contact
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE = Path(".sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
_raw_skip = os.getenv("SKIP_DOMAINS", "")
SKIP_DOMAINS: set[str] = {d.strip() for d in _raw_skip.split(",") if d.strip()}


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_ids": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def _build_hubspot_properties(contact: dict) -> dict:
    props: dict = {
        "email": contact["email"],
        "lead_source": "Gmail",
    }
    if contact["first_name"]:
        props["firstname"] = contact["first_name"]
    if contact["last_name"]:
        props["lastname"] = contact["last_name"]
    if contact["company"]:
        props["company"] = contact["company"]
    return props


def sync_contact(hubspot: HubSpotClient, contact: dict) -> dict:
    """Create or update a HubSpot contact. Returns a result summary dict."""
    email = contact["email"]
    existing = hubspot.find_contact_by_email(email)
    properties = _build_hubspot_properties(contact)

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        # Only patch fields that are currently blank
        patch = {
            k: v
            for k, v in properties.items()
            if v and not existing_props.get(k)
        }
        if patch:
            hubspot.update_contact(contact_id, patch)
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        result = hubspot.create_contact(properties)
        contact_id = result["id"]
        status = "Creato"

    # Timeline note (best-effort)
    try:
        hubspot.add_inbound_email_note(contact_id, email, contact["subject"])
    except Exception as exc:
        log.warning("Timeline note fallita per %s: %s", email, exc)

    return {"status": status, "email": email, "hubspot_id": contact_id}


def process_messages(
    gmail: GmailClient,
    hubspot: HubSpotClient,
    messages: list,
    state: dict,
) -> list:
    processed: set = set(state.get("processed_ids", []))
    results = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed:
            continue

        try:
            headers = gmail.get_message_headers(msg_id)
            contact = extract_contact(headers)

            if not contact:
                log.debug("Messaggio %s: nessun mittente valido, saltato", msg_id)
                processed.add(msg_id)
                continue

            if contact["domain"] in SKIP_DOMAINS:
                log.debug("Saltato %s (dominio escluso)", contact["email"])
                processed.add(msg_id)
                continue

            result = sync_contact(hubspot, contact)
            results.append(result)

            log.info(
                "[%-10s] %-45s → HubSpot ID: %s",
                result["status"],
                result["email"],
                result["hubspot_id"],
            )
        except Exception as exc:
            log.error("Errore sul messaggio %s: %s", msg_id, exc)

        processed.add(msg_id)

    # Cap the in-memory set to avoid unbounded growth
    if len(processed) > 10_000:
        processed = set(list(processed)[-5_000:])

    state["processed_ids"] = list(processed)
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        raise SystemExit("Errore: HUBSPOT_ACCESS_TOKEN non configurato in .env")

    gmail = GmailClient()
    hubspot = HubSpotClient(hubspot_token)
    state = _load_state()

    log.info("Gmail → HubSpot Sync avviato (polling ogni %ds)", POLL_INTERVAL)

    if not state["last_history_id"]:
        state["last_history_id"] = gmail.get_current_history_id()
        _save_state(state)
        log.info("Stato inizializzato. History ID: %s", state["last_history_id"])
        log.info("In attesa di nuove email...")

    while True:
        try:
            messages, new_history_id = gmail.get_new_messages(state["last_history_id"])

            if messages:
                log.info("Trovate %d nuove email da elaborare", len(messages))
                results = process_messages(gmail, hubspot, messages, state)

                if results:
                    created = sum(1 for r in results if r["status"] == "Creato")
                    updated = sum(1 for r in results if r["status"] == "Aggiornato")
                    ignored = sum(1 for r in results if r["status"] == "Ignorato")
                    log.info(
                        "Riepilogo: %d creati, %d aggiornati, %d ignorati",
                        created, updated, ignored,
                    )

            if new_history_id:
                state["last_history_id"] = new_history_id

            _save_state(state)

        except KeyboardInterrupt:
            log.info("Sync interrotto dall'utente.")
            _save_state(state)
            break
        except Exception as exc:
            log.error("Errore nel ciclo principale: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
