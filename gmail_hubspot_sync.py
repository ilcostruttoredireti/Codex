#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors incoming Gmail messages and syncs sender contacts to HubSpot CRM.
Supports one-shot and daemon (continuous polling) modes.

Usage:
  python gmail_hubspot_sync.py              # single run
  python gmail_hubspot_sync.py --daemon     # continuous polling (default: every 5 min)
  python gmail_hubspot_sync.py --daemon --interval 120
"""

import os
import json
import time
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
STATE_FILE = Path(".sync_state.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Domains considered personal (no company inferred from them)
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.uk", "yahoo.it",
    "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com",
    "libero.it", "tiscali.it", "virgilio.it", "alice.it", "tin.it",
    "protonmail.com", "proton.me",
}

# ---------------------------------------------------------------------------
# Persistent state  (tracks message IDs already processed)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        data["processed_ids"] = set(data.get("processed_ids", []))
        return data
    return {"last_sync_ts": None, "processed_ids": set()}


def save_state(state: dict) -> None:
    out = state.copy()
    out["processed_ids"] = list(state["processed_ids"])
    STATE_FILE.write_text(json.dumps(out, indent=2))

# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_gmail_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found: {CREDENTIALS_FILE}\n"
                    "Download OAuth2 credentials from Google Cloud Console "
                    "and save as credentials.json (or set GMAIL_CREDENTIALS_FILE)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def list_inbox_message_ids(service, since_ts: int | None) -> list[str]:
    """Return IDs of inbox messages newer than since_ts (Unix epoch)."""
    query = "in:inbox -from:me -category:promotions -category:social -category:updates"
    if since_ts:
        query += f" after:{since_ts}"

    ids: list[str] = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=100, pageToken=page_token)
            .execute()
        )
        for m in resp.get("messages", []):
            ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return ids


def fetch_from_header(service, msg_id: str) -> str | None:
    """Fetch only the From header of a single message (minimal quota usage)."""
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From"],
        )
        .execute()
    )
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"] == "From":
            return h["value"]
    return None

# ---------------------------------------------------------------------------
# Sender parsing
# ---------------------------------------------------------------------------

def parse_sender(from_header: str) -> dict | None:
    """
    Parse a raw From header into structured contact fields.
    Returns None if the address is invalid.
    """
    display_name, addr = parseaddr(from_header)
    if not addr or "@" not in addr:
        return None

    addr = addr.lower().strip()
    try:
        local_part, domain = addr.rsplit("@", 1)
    except ValueError:
        return None

    if not local_part or not domain or "." not in domain:
        return None

    display_name = display_name.strip()
    first = last = ""
    if display_name:
        parts = display_name.split(maxsplit=1)
        first = parts[0]
        if len(parts) > 1:
            last = parts[1]

    # Infer company from domain for non-personal addresses
    company = ""
    if domain not in PERSONAL_DOMAINS:
        base = domain.split(".")[0]
        company = base.replace("-", " ").replace("_", " ").capitalize()

    return {
        "email": addr,
        "first_name": first,
        "last_name": last,
        "domain": domain,
        "company": company,
    }

# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def build_hubspot_client():
    import hubspot

    if not HUBSPOT_TOKEN:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN environment variable is not set. "
            "Create a Private App in HubSpot and set the token."
        )
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact_by_email(hs, email: str):
    """Return the HubSpot contact object for this email, or None."""
    from hubspot.crm.contacts.models import (
        Filter,
        FilterGroup,
        PublicObjectSearchRequest,
    )

    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    result = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    return result.results[0] if result.total > 0 else None


def create_hubspot_contact(hs, sender: dict) -> str:
    """Create a new contact and return its HubSpot ID."""
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    props: dict[str, str] = {
        "email": sender["email"],
        "hs_lead_source": "Gmail",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    resp = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=props
        )
    )
    return resp.id


def update_hubspot_contact(hs, contact_id: str, sender: dict, existing) -> bool:
    """
    Fill in only blank fields on an existing contact.
    Returns True if any field was updated, False if nothing changed.
    """
    from hubspot.crm.contacts import SimplePublicObjectInput

    ep = existing.properties or {}
    updates: dict[str, str] = {}

    if sender["first_name"] and not ep.get("firstname"):
        updates["firstname"] = sender["first_name"]
    if sender["last_name"] and not ep.get("lastname"):
        updates["lastname"] = sender["last_name"]
    if sender["company"] and not ep.get("company"):
        updates["company"] = sender["company"]
    if not ep.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if not updates:
        return False

    hs.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return True

# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------

def process_message(
    gmail_service,
    hs,
    msg_id: str,
    processed_ids: set,
) -> dict:
    """
    Process one Gmail message.
    Returns a result dict with keys: status, email, contact_id, reason.
    """
    if msg_id in processed_ids:
        return _result("Ignorato", reason="già processato")

    processed_ids.add(msg_id)

    from_header = fetch_from_header(gmail_service, msg_id)
    if not from_header:
        return _result("Ignorato", reason="nessun header From")

    sender = parse_sender(from_header)
    if not sender:
        return _result("Ignorato", reason="indirizzo non valido", email=from_header)

    existing = find_contact_by_email(hs, sender["email"])

    if existing:
        updated = update_hubspot_contact(hs, existing.id, sender, existing)
        return _result(
            "Aggiornato" if updated else "Ignorato",
            email=sender["email"],
            contact_id=existing.id,
            reason="" if updated else "nessun campo da aggiornare",
        )

    contact_id = create_hubspot_contact(hs, sender)
    return _result("Creato", email=sender["email"], contact_id=contact_id)


def _result(
    status: str,
    email: str = "",
    contact_id: str = "",
    reason: str = "",
) -> dict:
    return {"status": status, "email": email, "contact_id": contact_id, "reason": reason}

# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------

def run_sync(gmail_service, hs, state: dict) -> list[dict]:
    since_ts = state.get("last_sync_ts")
    ids = list_inbox_message_ids(gmail_service, since_ts)
    log.info(f"Messaggi trovati: {len(ids)}")

    results: list[dict] = []
    for msg_id in ids:
        try:
            r = process_message(gmail_service, hs, msg_id, state["processed_ids"])
            results.append(r)
            if r["email"]:
                note = f"  ({r['reason']})" if r["reason"] else ""
                log.info(
                    f"[{r['status']:10}] {r['email']:45}  ID HubSpot: {r['contact_id'] or '-'}{note}"
                )
        except Exception as exc:
            log.error(f"Errore su messaggio {msg_id}: {exc}")
            results.append(_result("Errore", reason=str(exc)))

    state["last_sync_ts"] = int(datetime.now(timezone.utc).timestamp())
    return results


def print_summary(results: list[dict]) -> None:
    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    skipped = sum(1 for r in results if r["status"] == "Ignorato")
    errors  = sum(1 for r in results if r["status"] == "Errore")
    log.info(
        f"Sync completato — Creati: {created}, Aggiornati: {updated}, "
        f"Ignorati: {skipped}, Errori: {errors}"
    )

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    ap.add_argument(
        "--daemon",
        action="store_true",
        help="Modalità continua (polling periodico)",
    )
    ap.add_argument(
        "--interval",
        type=int,
        default=300,
        metavar="SEC",
        help="Intervallo di polling in secondi (default: 300)",
    )
    args = ap.parse_args()

    gmail_service = build_gmail_service()
    hs = build_hubspot_client()
    state = load_state()

    if args.daemon:
        log.info(f"Modalità daemon attiva — polling ogni {args.interval}s")
        while True:
            try:
                results = run_sync(gmail_service, hs, state)
                save_state(state)
                print_summary(results)
            except Exception as exc:
                log.error(f"Errore durante il sync: {exc}")
            time.sleep(args.interval)
    else:
        results = run_sync(gmail_service, hs, state)
        save_state(state)
        print_summary(results)

        # Human-readable table
        print()
        print(f"{'Stato':<12} {'Email mittente':<45} {'ID HubSpot'}")
        print("─" * 75)
        for r in results:
            if r["email"]:
                print(f"{r['status']:<12} {r['email']:<45} {r['contact_id'] or '-'}")
        print()


if __name__ == "__main__":
    main()
