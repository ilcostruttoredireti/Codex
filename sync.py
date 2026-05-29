#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
-----------------------------
Polls Gmail for new inbound emails and upserts sender contacts into HubSpot.

Usage:
    python sync.py

Environment variables (set in .env or shell):
    HUBSPOT_ACCESS_TOKEN   HubSpot Private App token       (required)
    GMAIL_CREDENTIALS      Path to OAuth credentials JSON  (default: credentials.json)
    GMAIL_TOKEN            Path to token cache file        (default: token.json)
    POLL_INTERVAL          Seconds between polls           (default: 300)
    ADD_TIMELINE_NOTES     Add Gmail note to HubSpot timeline (default: true)
"""

import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient, HubSpotError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))
ADD_TIMELINE = os.getenv("ADD_TIMELINE_NOTES", "true").lower() != "false"
STATE_FILE = Path("state.json")

# Domains treated as personal addresses — company field left empty
_PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "tim.it",
}

# Local-part prefixes that indicate automated/system senders — skip them
_SKIP_PREFIXES = {
    "noreply", "no-reply", "mailer-daemon", "postmaster",
    "bounce", "notifications", "donotreply", "do-not-reply",
    "support", "info", "admin",
}


# ------------------------------------------------------------------ #
# State management
# ------------------------------------------------------------------ #

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"history_id": None, "processed": []}


def _save_state(state: dict) -> None:
    # Cap the processed list to the last 2000 entries
    state["processed"] = state["processed"][-2000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _company_from_domain(domain: str) -> str | None:
    """Derive a best-guess company name from an email domain."""
    if not domain or domain in _PERSONAL_DOMAINS:
        return None
    # strip TLD(s) and return the registrable label, capitalised
    parts = domain.split(".")
    return parts[-2].capitalize() if len(parts) >= 2 else domain.capitalize()


def _should_skip(sender: dict) -> bool:
    local = sender["email"].split("@")[0].lower().replace(".", "").replace("-", "").replace("_", "")
    return any(prefix in local for prefix in _SKIP_PREFIXES)


# ------------------------------------------------------------------ #
# Core upsert logic
# ------------------------------------------------------------------ #

def upsert_contact(hs: HubSpotClient, sender: dict) -> dict:
    """
    Create or update a HubSpot contact from sender info.

    Returns:
        {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "contact_id": ...}
    """
    email = sender["email"]

    if _should_skip(sender):
        return {"status": "Ignorato", "email": email, "contact_id": None}

    company = _company_from_domain(sender["domain"])
    existing = hs.find_contact(email)

    if existing:
        contact_id = existing["id"]
        ep = existing.get("properties", {})

        # Fill in only blank fields — never overwrite existing data
        updates: dict = {}
        if sender["first_name"] and not ep.get("firstname"):
            updates["firstname"] = sender["first_name"]
        if sender["last_name"] and not ep.get("lastname"):
            updates["lastname"] = sender["last_name"]
        if company and not ep.get("company"):
            updates["company"] = company

        if updates:
            hs.update_contact(contact_id, updates)

        if ADD_TIMELINE:
            hs.add_email_note(contact_id, email, sender["message_id"])

        return {"status": "Aggiornato", "email": email, "contact_id": contact_id}

    # --- create ---
    props: dict = {"email": email, "lead_source": "Gmail"}
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if company:
        props["company"] = company

    created = hs.create_contact(props)
    contact_id = created["id"]

    if ADD_TIMELINE:
        hs.add_email_note(contact_id, email, sender["message_id"])

    return {"status": "Creato", "email": email, "contact_id": contact_id}


# ------------------------------------------------------------------ #
# Main loop
# ------------------------------------------------------------------ #

def _validate_env() -> None:
    if not os.getenv("HUBSPOT_ACCESS_TOKEN"):
        raise SystemExit("Errore: variabile HUBSPOT_ACCESS_TOKEN non impostata.")


def run() -> None:
    _validate_env()

    gmail = GmailClient(
        credentials_file=os.getenv("GMAIL_CREDENTIALS", "credentials.json"),
        token_file=os.getenv("GMAIL_TOKEN", "token.json"),
    )
    hs = HubSpotClient(access_token=os.getenv("HUBSPOT_ACCESS_TOKEN"))

    log.info("Sync Gmail → HubSpot avviato (polling ogni %d s)", POLL_INTERVAL)

    while True:
        try:
            state = _load_state()

            # ---- fetch new message IDs --------------------------------
            if state["history_id"]:
                try:
                    msg_ids, new_hid = gmail.get_new_message_ids(state["history_id"])
                except Exception as exc:
                    # historyId expired (>7 days) — reset and fall back
                    log.warning("history API error (%s), reset a messaggi recenti.", exc)
                    msg_ids = gmail.get_recent_message_ids()
                    new_hid = gmail.get_history_id()
            else:
                # First run — seed with recent messages
                msg_ids = gmail.get_recent_message_ids()
                new_hid = gmail.get_history_id()

            # ---- filter already processed ----------------------------
            pending = [m for m in msg_ids if m not in state["processed"]]

            if pending:
                log.info("%d nuovi messaggi da processare.", len(pending))
            else:
                log.debug("Nessun nuovo messaggio.")

            # ---- process each message --------------------------------
            for msg_id in pending:
                try:
                    sender = gmail.get_sender(msg_id)
                    if sender is None:
                        state["processed"].append(msg_id)
                        continue

                    result = upsert_contact(hs, sender)
                    state["processed"].append(msg_id)

                    log.info(
                        "%-10s | %-45s | HubSpot ID: %s",
                        result["status"],
                        result["email"],
                        result["contact_id"] or "-",
                    )
                except HubSpotError as exc:
                    log.error("HubSpot error per msg %s: %s", msg_id, exc)
                except Exception as exc:
                    log.error("Errore inatteso per msg %s: %s", msg_id, exc)

            state["history_id"] = new_hid
            _save_state(state)

        except Exception as exc:
            log.error("Errore nel ciclo principale: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
