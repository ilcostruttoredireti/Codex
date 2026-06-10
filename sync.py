#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot automatically.

Usage:
    python sync.py              # Continuous polling (60s interval)
    python sync.py --once       # Single run and exit
    python sync.py --interval 120   # Custom polling interval in seconds
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts.models import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".sync_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Sender prefixes and domains to skip (automated/system accounts)
_IGNORED_PREFIXES = frozenset({
    "noreply", "no-reply", "do-not-reply", "donotreply",
    "mailer-daemon", "postmaster", "bounce", "notifications",
    "newsletter", "support", "info", "unsubscribe",
})
_IGNORED_DOMAINS = frozenset({
    "noreply.github.com", "notifications.github.com",
    "bounce.amazon.com", "mailer.stripe.com",
})
_COMMON_TLDS = re.compile(
    r"\.(com|it|net|org|io|co|eu|de|fr|es|uk|biz|info|app|dev|ai|tech)$",
    re.IGNORECASE,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"processed_ids": [], "last_run": None}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def _domain_to_company(domain: str) -> str:
    """Heuristic: 'acme-group.co.uk' → 'Acme Group'."""
    # strip port if any
    domain = domain.split(":")[0]
    # remove www.
    domain = re.sub(r"^www\.", "", domain, flags=re.IGNORECASE)
    # strip TLD(s)
    domain = _COMMON_TLDS.sub("", domain)
    domain = _COMMON_TLDS.sub("", domain)  # handle .co.uk double-pass
    return domain.replace("-", " ").replace("_", " ").title()


def _should_skip(email: str) -> bool:
    email = email.lower()
    prefix, _, domain = email.partition("@")
    if not domain:
        return True
    if any(prefix.startswith(p) for p in _IGNORED_PREFIXES):
        return True
    if domain in _IGNORED_DOMAINS:
        return True
    return False


# ── Gmail ────────────────────────────────────────────────────────────────────

def _build_gmail_service():
    token_path = Path("token.json")
    creds_path = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at '{creds_path}'.\n"
                    "Download credentials.json from Google Cloud Console → "
                    "APIs & Services → OAuth 2.0 Client IDs."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _fetch_new_messages(service, processed: set, max_results: int = 50) -> list[dict]:
    """Return inbox messages not yet processed."""
    try:
        resp = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=max_results,
        ).execute()
        messages = resp.get("messages", [])
        return [m for m in messages if m["id"] not in processed]
    except HttpError as exc:
        logger.error("Gmail list error: %s", exc)
        return []


def _parse_sender(service, message_id: str) -> dict | None:
    """Fetch message headers and return a sender dict, or None if invalid."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except HttpError as exc:
        logger.error("Gmail get error for %s: %s", message_id, exc)
        return None

    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    raw_from = headers.get("From", "")
    display_name, email = parseaddr(raw_from)

    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    if _should_skip(email):
        return None

    domain = email.split("@")[1]
    first_name, last_name = _split_name(display_name) if display_name else ("", "")

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company": _domain_to_company(domain),
        "domain": domain,
        "subject": headers.get("Subject", ""),
        "received_at": headers.get("Date", ""),
        "message_id": message_id,
    }


# ── HubSpot ──────────────────────────────────────────────────────────────────

def _build_hubspot_client():
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN not set. "
            "Create a Private App in HubSpot Settings → Integrations → Private Apps."
        )
    return hubspot.Client.create(access_token=token)


def _find_contact(client, email: str):
    """Return the HubSpot contact object for this email, or None."""
    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(
                        filters=[Filter(property_name="email", operator="EQ", value=email)]
                    )
                ],
                properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
                limit=1,
            )
        )
        return result.results[0] if result.total > 0 else None
    except ApiException as exc:
        logger.error("HubSpot search error (%s): %s", email, exc)
        return None


def _create_contact(client, sender: dict) -> tuple[str, str]:
    props = {"email": sender["email"], "hs_lead_source": "Gmail"}
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return "Creato", result.id
    except ApiException as exc:
        body = {}
        try:
            body = json.loads(exc.body) if exc.body else {}
        except (json.JSONDecodeError, TypeError):
            pass
        if body.get("category") == "CONFLICT":
            # Created by concurrent run — find and return existing
            existing = _find_contact(client, sender["email"])
            if existing:
                return "Ignorato", existing.id
        logger.error("HubSpot create error (%s): %s", sender["email"], exc)
        return "Errore", "N/A"


def _update_contact(client, contact_id: str, sender: dict, existing) -> tuple[str, str]:
    """Fill in any blank fields on the existing contact."""
    p = existing.properties
    updates: dict[str, str] = {}

    if not p.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not p.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not p.get("company") and sender["company"]:
        updates["company"] = sender["company"]
    if not p.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if not updates:
        return "Ignorato", contact_id

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return "Aggiornato", contact_id
    except ApiException as exc:
        logger.error("HubSpot update error (%s): %s", contact_id, exc)
        return "Errore", contact_id


def _sync_sender(client, sender: dict) -> tuple[str, str, str]:
    """Create or update one contact. Returns (stato, email, hubspot_id)."""
    existing = _find_contact(client, sender["email"])
    if existing:
        status, cid = _update_contact(client, existing.id, sender, existing)
    else:
        status, cid = _create_contact(client, sender)
    return status, sender["email"], cid


# ── main loop ────────────────────────────────────────────────────────────────

def run(poll_interval: int = 60, once: bool = False) -> None:
    logger.info("Inizializzazione Gmail → HubSpot sync...")
    gmail = _build_gmail_service()
    hs = _build_hubspot_client()

    state = _load_state()
    processed: set[str] = set(state.get("processed_ids", []))

    header = "Gmail → HubSpot Contact Sync"
    print(f"\n{'═' * 60}")
    print(f"  {header}")
    print(f"  Avviato: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Modalità: {'singola esecuzione' if once else f'polling ogni {poll_interval}s'}")
    print(f"{'═' * 60}\n")

    try:
        while True:
            logger.info("Controllo nuove email in arrivo...")
            messages = _fetch_new_messages(gmail, processed)

            if messages:
                logger.info("Trovate %d email da processare.", len(messages))
                results: list[tuple[str, str, str]] = []

                for msg in messages:
                    mid = msg["id"]
                    sender = _parse_sender(gmail, mid)
                    processed.add(mid)  # mark regardless — avoid retrying broken messages

                    if not sender:
                        logger.debug("Messaggio %s ignorato (mittente non valido).", mid)
                        continue

                    status, email, cid = _sync_sender(hs, sender)
                    results.append((status, email, cid))

                if results:
                    print(f"\n{'─' * 62}")
                    print(f"  {'Stato':<12} {'Email':<34} {'HubSpot ID'}")
                    print(f"{'─' * 62}")
                    for status, email, cid in results:
                        print(f"  {status:<12} {email:<34} {cid}")
                    print(f"{'─' * 62}\n")
                else:
                    logger.info("Tutti i mittenti ignorati (account automatici).")

                # Persist state; cap at 10 000 IDs to prevent unbounded growth
                state["processed_ids"] = list(processed)[-10_000:]
                state["last_run"] = datetime.now(timezone.utc).isoformat()
                _save_state(state)
            else:
                logger.info("Nessuna nuova email.")

            if once:
                break

            logger.info("Prossimo controllo tra %ds. Premi Ctrl+C per uscire.", poll_interval)
            time.sleep(poll_interval)

    except KeyboardInterrupt:
        logger.info("Sync interrotto dall'utente.")
        state["processed_ids"] = list(processed)[-10_000:]
        _save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gmail → HubSpot Contact Sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui una singola scansione e termina (default: loop continuo)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        metavar="SECONDI",
        help="Intervallo di polling in secondi (default: 60)",
    )
    args = parser.parse_args()
    run(poll_interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
