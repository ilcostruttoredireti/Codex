#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

For every new inbox email the script:
  1. Extracts the sender's email, name, and company domain.
  2. Searches HubSpot for an existing contact by email.
  3. Creates a new contact or fills in any missing fields on the existing one.
  4. Logs an inbound-email activity on the contact's timeline.
  5. Applies the "hubspot-synced" Gmail label so the message is never
     re-processed across restarts.

Required environment variables (see .env.example):
  HUBSPOT_ACCESS_TOKEN   HubSpot Private App token
  GMAIL_CREDENTIALS_FILE Path to the OAuth client-secret JSON (default: credentials.json)
  GMAIL_TOKEN_FILE       Path to the cached token file (default: token.json)

Optional:
  POLL_INTERVAL          Seconds between Gmail polls (default: 60)
  LOOKBACK_DAYS          How far back to look on first run (default: 7, 0 = all)
"""

import logging
import os
import sys
import time

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

SYNCED_LABEL = "hubspot-synced"
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "protonmail.com", "aol.com", "me.com", "msn.com",
    "mac.com", "ymail.com", "googlemail.com", "fastmail.com",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(maxsplit=1)
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def company_from_domain(email: str) -> str:
    """Return a capitalised company name derived from the email domain,
    or an empty string for generic/consumer providers."""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if not domain or domain in GENERIC_DOMAINS:
        return ""
    return domain.split(".")[0].capitalize()


def build_create_props(sender: dict) -> dict:
    first, last = split_name(sender.get("name", ""))
    company = company_from_domain(sender["email"])

    props: dict[str, str] = {"email": sender["email"], "leadsource": "Gmail"}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company
    return props


def build_update_props(sender: dict, existing_props: dict) -> dict:
    """Return only the fields that are currently blank in HubSpot."""
    first, last = split_name(sender.get("name", ""))
    company = company_from_domain(sender["email"])

    updates: dict[str, str] = {}
    if first and not existing_props.get("firstname"):
        updates["firstname"] = first
    if last and not existing_props.get("lastname"):
        updates["lastname"] = last
    if company and not existing_props.get("company"):
        updates["company"] = company
    return updates


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process(sender: dict, gmail: GmailClient, hs: HubSpotClient, label_id: str) -> dict:
    email = sender.get("email", "").strip()

    if not email or "@" not in email:
        log.warning("Skipping message %s – invalid sender address", sender.get("message_id"))
        return {"status": "Ignorato", "email": email or "(vuoto)", "contact_id": None}

    existing = hs.find_by_email(email)

    if existing:
        contact_id: str = existing["id"]
        updates = build_update_props(sender, existing.get("properties", {}))
        if updates:
            hs.update_contact(contact_id, updates)
        status = "Aggiornato"
    else:
        created = hs.create_contact(build_create_props(sender))
        contact_id = created["id"]
        status = "Creato"

    hs.log_email_activity(contact_id, email, sender.get("subject", ""))
    gmail.mark_synced(sender["message_id"], label_id)

    return {"status": status, "email": email, "contact_id": contact_id}


def log_result(result: dict) -> None:
    log.info(
        "Stato: %-10s | Email: %-42s | HubSpot ID: %s",
        result["status"],
        result["email"],
        result["contact_id"] or "—",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(gmail: GmailClient, hs: HubSpotClient, poll_interval: int, lookback_days: int) -> None:
    label_id = gmail.ensure_label(SYNCED_LABEL)
    log.info("Sync avviato — polling ogni %ds, lookback %d giorni", poll_interval, lookback_days)

    while True:
        try:
            messages = gmail.list_unsynced(SYNCED_LABEL, lookback_days)
            if messages:
                log.info("%d nuov%s email da elaborare", len(messages), "a" if len(messages) == 1 else "e")
                for msg in messages:
                    sender = gmail.get_sender(msg["id"])
                    result = process(sender, gmail, hs, label_id)
                    log_result(result)
            else:
                log.debug("Nessuna nuova email.")
        except KeyboardInterrupt:
            log.info("Interruzione ricevuta — uscita.")
            break
        except Exception as exc:
            log.error("Errore durante il sync: %s", exc)

        time.sleep(poll_interval)


def main() -> None:
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    poll_interval = int(os.getenv("POLL_INTERVAL", "60"))
    lookback_days = int(os.getenv("LOOKBACK_DAYS", "7"))

    if not hubspot_token:
        sys.exit("Errore: HUBSPOT_ACCESS_TOKEN non impostato. Vedi .env.example.")
    if not os.path.exists(credentials_file):
        sys.exit(
            f"Errore: file credenziali Gmail non trovato: {credentials_file}\n"
            "Scaricalo da Google Cloud Console > API e servizi > Credenziali > OAuth 2.0."
        )

    gmail = GmailClient(credentials_file, token_file)
    hs = HubSpotClient(hubspot_token)
    run(gmail, hs, poll_interval, lookback_days)


if __name__ == "__main__":
    main()
