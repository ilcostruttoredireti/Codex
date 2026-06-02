"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously and syncs senders as HubSpot contacts.

Usage:
    python sync.py [--once]

    --once   Process emails once and exit (no loop)
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from gmail_client import GmailClient, is_skippable
from hubspot_client import HubSpotClient, build_contact_props
from state import SyncState

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_LABEL = "Synced/HubSpot"


def _get_config() -> dict:
    required = {
        "GMAIL_CREDENTIALS_FILE": os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        "GMAIL_TOKEN_FILE": os.getenv("GMAIL_TOKEN_FILE", "token.json"),
        "HUBSPOT_ACCESS_TOKEN": os.getenv("HUBSPOT_ACCESS_TOKEN"),
    }
    if not required["HUBSPOT_ACCESS_TOKEN"]:
        sys.exit("ERROR: HUBSPOT_ACCESS_TOKEN is not set. Add it to your .env file.")

    skip_raw = os.getenv("SKIP_DOMAINS", "noreply.com,no-reply.com,mailer-daemon.com")
    skip_domains = {d.strip().lower() for d in skip_raw.split(",") if d.strip()}

    return {
        **required,
        "poll_interval": int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        "state_file": os.getenv("STATE_FILE", "sync_state.json"),
        "skip_domains": skip_domains,
    }


def process_emails(gmail: GmailClient, hubspot: HubSpotClient,
                   state: SyncState, skip_domains: set,
                   synced_label_id: str) -> list[dict]:
    """
    Fetch new inbox emails, sync senders to HubSpot.
    Returns a list of result records per processed email.
    """
    messages = gmail.get_inbox_messages(after_timestamp=state.last_timestamp or None)
    if not messages:
        log.debug("No new messages found.")
        return []

    results = []
    max_ts = state.last_timestamp

    for msg_stub in messages:
        msg_id = msg_stub["id"]

        if state.is_processed(msg_id):
            continue

        sender = gmail.get_message_details(msg_id)
        if not sender:
            state.mark_processed(msg_id, 0)
            continue

        ts = sender["internal_date"]
        if ts > max_ts:
            max_ts = ts

        email_addr = sender["email"]

        if is_skippable(email_addr, skip_domains):
            log.debug("Skipping automated sender: %s", email_addr)
            state.mark_processed(msg_id, ts)
            results.append({"status": "Ignorato", "email": email_addr, "hubspot_id": None})
            continue

        result = _sync_contact(hubspot, sender)
        result["email"] = email_addr

        if result["status"] != "Errore":
            try:
                gmail.apply_label(msg_id, synced_label_id)
            except Exception:
                pass

        state.mark_processed(msg_id, ts)
        results.append(result)

        _log_result(result)

    if max_ts > state.last_timestamp:
        state.set_last_timestamp(max_ts)

    return results


def _sync_contact(hubspot: HubSpotClient, sender: dict) -> dict:
    """Create or update a HubSpot contact for the given sender."""
    email_addr = sender["email"]

    try:
        existing = hubspot.find_contact_by_email(email_addr)

        if existing:
            contact_id = existing["id"]
            updates = build_contact_props(sender, existing_props=existing["properties"])
            if updates:
                hubspot.update_contact(contact_id, updates)
            hubspot.add_email_activity(
                contact_id, sender["subject"], email_addr, sender["date"]
            )
            return {"status": "Aggiornato", "hubspot_id": contact_id}
        else:
            props = build_contact_props(sender)
            created = hubspot.create_contact(props)
            contact_id = created["id"]
            hubspot.add_email_activity(
                contact_id, sender["subject"], email_addr, sender["date"]
            )
            return {"status": "Creato", "hubspot_id": contact_id}

    except Exception as exc:
        log.error("Failed to sync %s: %s", email_addr, exc)
        return {"status": "Errore", "hubspot_id": None}


def _log_result(result: dict):
    status = result["status"]
    email_addr = result["email"]
    contact_id = result.get("hubspot_id", "-")
    log.info("%-12s | %-40s | ID: %s", status, email_addr, contact_id or "-")


def run_sync_loop(config: dict, once: bool = False):
    log.info("Initializing Gmail client...")
    gmail = GmailClient(
        credentials_file=config["GMAIL_CREDENTIALS_FILE"],
        token_file=config["GMAIL_TOKEN_FILE"],
    )
    log.info("Initializing HubSpot client...")
    hubspot = HubSpotClient(access_token=config["HUBSPOT_ACCESS_TOKEN"])
    state = SyncState(state_file=config["state_file"])
    synced_label_id = gmail.ensure_label(GMAIL_LABEL)

    log.info(
        "Sync started. Poll interval: %ds | Skip domains: %s",
        config["poll_interval"],
        ", ".join(sorted(config["skip_domains"])) or "none",
    )

    while True:
        log.info("--- Checking for new emails ---")
        results = process_emails(gmail, hubspot, state, config["skip_domains"], synced_label_id)

        created = sum(1 for r in results if r["status"] == "Creato")
        updated = sum(1 for r in results if r["status"] == "Aggiornato")
        skipped = sum(1 for r in results if r["status"] == "Ignorato")

        if results:
            log.info("Batch done: %d creati, %d aggiornati, %d ignorati", created, updated, skipped)
        else:
            log.info("Nessuna nuova email da processare.")

        if once:
            break

        log.info("Prossimo check tra %d secondi...", config["poll_interval"])
        time.sleep(config["poll_interval"])


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true",
                        help="Run a single sync pass and exit")
    args = parser.parse_args()

    config = _get_config()
    run_sync_loop(config, once=args.once)


if __name__ == "__main__":
    main()
