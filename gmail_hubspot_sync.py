#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Continuously monitors incoming Gmail emails and syncs sender contacts to HubSpot.
"""

import os
import re
import json
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path
from email.utils import parseaddr

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    PublicObjectSearchRequest,
)
from hubspot.crm.contacts.exceptions import ApiException

# ── Configuration ──────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
CREDENTIALS_FILE = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path(os.getenv("GOOGLE_TOKEN_FILE", "token.json"))
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Free email providers — domain is NOT a company name
FREE_PROVIDERS = {
    "gmail", "yahoo", "hotmail", "outlook", "icloud",
    "protonmail", "live", "msn", "aol", "libero", "tiscali",
}

# Senders to always skip (automated systems)
SKIP_PATTERNS = [
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "postmaster", "bounce", "notification", "alerts",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail Auth ────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ── State Management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_message_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Email Parsing ─────────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> dict | None:
    name, email = parseaddr(from_header)
    if not email or "@" not in email:
        return None

    email = email.strip().lower()
    name = name.strip()
    domain = email.split("@")[1]

    parts = name.split(" ", 1) if name else []
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""

    # Derive company from domain when it's not a free provider
    base_domain = domain.split(".")[-2] if domain.count(".") >= 1 else domain
    company = base_domain.capitalize() if base_domain not in FREE_PROVIDERS else ""

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "name": name,
        "domain": domain,
        "company": company,
    }


def is_automated_sender(email: str) -> bool:
    return any(p in email for p in SKIP_PATTERNS)


# ── Gmail Fetching ────────────────────────────────────────────────────────────

def fetch_new_messages(service, processed_ids: set) -> list[dict]:
    results = []
    page_token = None

    while True:
        kwargs: dict = {
            "userId": "me",
            "q": "in:inbox -from:me",
            "maxResults": 50,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        messages = response.get("messages", [])

        for ref in messages:
            if ref["id"] in processed_ids:
                continue
            msg = service.users().messages().get(
                userId="me",
                id=ref["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            results.append({
                "id": ref["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            })

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


# ── HubSpot Operations ────────────────────────────────────────────────────────

def find_contact(client, email: str):
    req = PublicObjectSearchRequest(
        filter_groups=[{
            "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
        }],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        return result.results[0] if result.total > 0 else None
    except ApiException as e:
        log.error("HubSpot search error for %s: %s", email, e)
        return None


def build_props(sender: dict) -> dict:
    props: dict = {
        "email": sender["email"],
        "leadsource": CONTACT_SOURCE,
        "hs_lead_status": CONTACT_TAG,
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]
    return props


def create_or_update_contact(client, sender: dict) -> tuple[str, str]:
    """Return (stato, hubspot_id). stato ∈ {'Creato', 'Aggiornato', 'Ignorato', 'Errore'}."""
    existing = find_contact(client, sender["email"])

    try:
        if existing:
            ep = existing.properties
            # Only write fields that are currently blank
            updates = {
                k: v for k, v in build_props(sender).items()
                if k != "email" and not ep.get(k) and v
            }
            if updates:
                client.crm.contacts.basic_api.update(
                    contact_id=existing.id,
                    simple_public_object_input=SimplePublicObjectInput(properties=updates),
                )
                return "Aggiornato", existing.id
            return "Ignorato", existing.id
        else:
            contact = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=build_props(sender)
                )
            )
            return "Creato", contact.id
    except ApiException as e:
        log.error("HubSpot error for %s: %s", sender["email"], e)
        return "Errore", ""


# ── Sync Cycle ────────────────────────────────────────────────────────────────

def sync_once(gmail_service, hubspot_client, state: dict) -> tuple[dict, list[dict]]:
    processed_set = set(state.get("processed_message_ids", []))
    messages = fetch_new_messages(gmail_service, processed_set)
    log.info("Nuove email trovate: %d", len(messages))

    report: list[dict] = []

    for msg in messages:
        sender = parse_sender(msg["from"])

        if not sender:
            log.warning("Mittente non parsabile: %s", msg["from"])
            processed_set.add(msg["id"])
            continue

        if is_automated_sender(sender["email"]):
            log.debug("Skip automatico: %s", sender["email"])
            report.append({"stato": "Ignorato", "email": sender["email"], "hubspot_id": "-"})
            processed_set.add(msg["id"])
            continue

        stato, hid = create_or_update_contact(hubspot_client, sender)
        report.append({"stato": stato, "email": sender["email"], "hubspot_id": hid})
        log.info("[%s] %s  →  HubSpot ID: %s", stato, sender["email"], hid)
        processed_set.add(msg["id"])

    # Keep at most 10 000 IDs to bound memory/disk
    state["processed_message_ids"] = list(processed_set)[-10_000:]
    return state, report


def print_report(report: list[dict]):
    if not report:
        print("Nessuna email processata in questo ciclo.\n")
        return
    print("\n┌─ Report Gmail → HubSpot " + "─" * 44)
    print(f"│ {'Stato':<12} {'Email':<38} {'HubSpot ID'}")
    print("│" + "─" * 65)
    for r in report:
        print(f"│ {r['stato']:<12} {r['email']:<38} {r['hubspot_id']}")
    totals = {}
    for r in report:
        totals[r["stato"]] = totals.get(r["stato"], 0) + 1
    summary = "  ".join(f"{v} {k}" for k, v in totals.items())
    print(f"└─ Totale: {len(report)} email  ({summary})\n")


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once", action="store_true",
        help="Esegui un solo ciclo e termina (utile per cron)"
    )
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL,
        help=f"Secondi tra un ciclo e l'altro (default: {POLL_INTERVAL})"
    )
    args = parser.parse_args()

    if not HUBSPOT_TOKEN:
        log.error("Variabile HUBSPOT_ACCESS_TOKEN non impostata. Esci.")
        raise SystemExit(1)

    if not CREDENTIALS_FILE.exists() and not TOKEN_FILE.exists():
        log.error(
            "Nessun file di credenziali Google trovato. "
            "Scarica credentials.json dalla Google Cloud Console."
        )
        raise SystemExit(1)

    gmail_service = get_gmail_service()
    hubspot_client = hubspot.Client.create(access_token=HUBSPOT_TOKEN)
    state = load_state()

    log.info(
        "Avvio sync Gmail → HubSpot  (intervallo: %ds  |  modalità: %s)",
        args.interval,
        "singolo ciclo" if args.once else "continua",
    )

    while True:
        try:
            state, report = sync_once(gmail_service, hubspot_client, state)
            save_state(state)
            print_report(report)
        except Exception as exc:
            log.error("Errore durante sync: %s", exc, exc_info=True)

        if args.once:
            break

        log.info("Prossimo ciclo tra %d secondi…", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
