#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs senders to HubSpot CRM.

Usage:
    python sync.py              # run once
    python sync.py --loop 60    # poll every 60 seconds
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".processed_emails.json")
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")

GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "libero.it", "live.it", "tiscali.it", "virgilio.it",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("sync.log")],
)
log = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail ─────────────────────────────────────────────────────────────────────


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


def fetch_unread_message_ids(svc, state: dict) -> list[str]:
    result = svc.users().messages().list(
        userId="me", q="in:inbox -from:me is:unread", maxResults=50
    ).execute()
    all_ids = [m["id"] for m in result.get("messages", [])]
    return [mid for mid in all_ids if mid not in state["processed"]]


def fetch_message_headers(svc, msg_id: str) -> dict:
    msg = svc.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


# ── Parsing ───────────────────────────────────────────────────────────────────


def parse_from_header(from_header: str) -> tuple[str, str, str]:
    """Return (email, firstname, lastname) from a From: header string."""
    m = re.match(r'^"?(.+?)"?\s*<([^>]+)>', from_header.strip())
    if m:
        display = m.group(1).strip()
        email = m.group(2).strip().lower()
        parts = display.split()
        return email, parts[0], " ".join(parts[1:])
    email = from_header.strip().lower()
    return email, "", ""


def company_from_domain(email: str) -> str:
    domain = email.split("@")[-1].lower()
    if domain in GENERIC_DOMAINS:
        return ""
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


# ── HubSpot ───────────────────────────────────────────────────────────────────


def hs_find_contact(client, email: str) -> Optional[object]:
    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
                "limit": 1,
            }
        )
        return resp.results[0] if resp.results else None
    except ApiException as exc:
        log.error("HubSpot search error for %s: %s", email, exc)
        return None


def hs_create_contact(client, email: str, first: str, last: str, company: str) -> Optional[str]:
    props = {"email": email, "hs_lead_source": "Gmail"}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company

    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return str(result.id)
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", email, exc)
        return None


def hs_update_contact(client, contact_id: str, updates: dict) -> bool:
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error for %s: %s", contact_id, exc)
        return False


# ── Core logic ────────────────────────────────────────────────────────────────


def process_message(svc, msg_id: str, hs_client, state: dict, own_email: str) -> dict:
    if msg_id in state["processed"]:
        return {"status": "Ignorato", "reason": "già processato"}

    try:
        headers = fetch_message_headers(svc, msg_id)
    except Exception as exc:
        return {"status": "Errore", "reason": str(exc)}

    from_header = headers.get("From", "")
    if not from_header:
        state["processed"].append(msg_id)
        return {"status": "Ignorato", "reason": "nessun mittente", "msg_id": msg_id}

    email, first, last = parse_from_header(from_header)

    if not email or "@" not in email:
        state["processed"].append(msg_id)
        return {"status": "Ignorato", "reason": "email non valida", "msg_id": msg_id}

    if email.lower() == own_email.lower():
        state["processed"].append(msg_id)
        return {"status": "Ignorato", "reason": "email propria", "email": email}

    company = company_from_domain(email)
    existing = hs_find_contact(hs_client, email)

    if existing:
        contact_id = str(existing.id)
        props = existing.properties or {}
        updates = {}
        if first and not props.get("firstname"):
            updates["firstname"] = first
        if last and not props.get("lastname"):
            updates["lastname"] = last
        if company and not props.get("company"):
            updates["company"] = company
        if not props.get("hs_lead_source"):
            updates["hs_lead_source"] = "Gmail"

        if updates:
            hs_update_contact(hs_client, contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        contact_id = hs_create_contact(hs_client, email, first, last, company)
        status = "Creato" if contact_id else "Errore"

    state["processed"].append(msg_id)
    return {
        "status": status,
        "email": email,
        "contact_id": contact_id,
        "subject": headers.get("Subject", ""),
    }


# ── Entry point ───────────────────────────────────────────────────────────────


def run(poll_interval: Optional[int] = None) -> None:
    hubspot_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        sys.exit("HUBSPOT_ACCESS_TOKEN non impostato.")
    own_email = os.environ.get("GMAIL_ADDRESS", "")

    gmail_svc = get_gmail_service()
    hs_client = hubspot.Client.create(access_token=hubspot_token)
    state = load_state()

    log.info("Sync avviato%s.", f" (polling ogni {poll_interval}s)" if poll_interval else " (run singolo)")

    def run_once() -> None:
        msg_ids = fetch_unread_message_ids(gmail_svc, state)
        log.info("%d email da processare.", len(msg_ids))
        for mid in msg_ids:
            res = process_message(gmail_svc, mid, hs_client, state, own_email)
            log.info(
                "Stato: %-10s | Email: %-40s | ID HubSpot: %s",
                res.get("status", "-"),
                res.get("email", "-"),
                res.get("contact_id", "-"),
            )
            save_state(state)

    if poll_interval:
        while True:
            try:
                run_once()
            except Exception as exc:
                log.error("Errore nel ciclo: %s", exc)
            time.sleep(poll_interval)
    else:
        run_once()


if __name__ == "__main__":
    interval = None
    if "--loop" in sys.argv:
        idx = sys.argv.index("--loop")
        interval = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 60
    run(interval)
