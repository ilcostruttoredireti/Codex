"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts into HubSpot.
Avoids duplicates (email as unique key), updates existing records,
creates new ones, and logs inbound activity as HubSpot Notes.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException as ContactApiException,
)
from hubspot.crm.contacts.api import BasicApi as ContactBasicApi
from hubspot.crm.contacts.api import SearchApi as ContactSearchApi
from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
from hubspot.crm.objects import ApiException as ObjectApiException
from hubspot.crm.objects.api import BasicApi as ObjectBasicApi

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json"))
GMAIL_CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "gmail_credentials.json"))
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"
OWN_EMAILS = set(e.strip() for e in os.getenv("OWN_EMAILS", "").split(",") if e.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── State ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail ────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_messages(service, query: str = "in:inbox", max_results: int = 50) -> list[dict]:
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    return result.get("messages", [])


def get_message(service, message_id: str) -> dict:
    return (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="metadata",
             metadataHeaders=["From", "Subject", "Date"])
        .execute()
    )


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Returns (email, firstname, lastname) parsed from a From: header.
    Examples:
        "Maya Amenduni <maya@example.com>"  → ("maya@example.com", "Maya", "Amenduni")
        "info@example.com"                  → ("info@example.com", "", "")
    """
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', from_header.strip())
    if match:
        display_name = match.group(1).strip()
        email = match.group(2).strip().lower()
        parts = display_name.split()
        firstname = parts[0] if parts else ""
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
        return email, firstname, lastname
    plain_email = from_header.strip().lower()
    return plain_email, "", ""


def domain_from_email(email: str) -> str:
    parts = email.split("@")
    if len(parts) != 2:
        return ""
    domain = parts[1]
    # Skip generic free providers
    if domain in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "libero.it"}:
        return ""
    return domain


def company_from_domain(domain: str) -> str:
    """Best-effort company name from a domain (strips TLD, capitalises)."""
    if not domain:
        return ""
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


# ── HubSpot ──────────────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(client: hubspot.Client, email: str) -> dict | None:
    from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source", "notes_last_contacted"],
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if resp.total > 0:
            return resp.results[0]
    except ContactApiException as e:
        log.error("HubSpot search error: %s", e)
    return None


def create_contact(client: hubspot.Client, props: dict) -> dict | None:
    obj = SimplePublicObjectInputForCreate(properties=props)
    try:
        return client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
    except ContactApiException as e:
        log.error("HubSpot create error: %s", e)
    return None


def update_contact(client: hubspot.Client, contact_id: str, props: dict) -> dict | None:
    obj = SimplePublicObjectInput(properties=props)
    try:
        return client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=obj,
        )
    except ContactApiException as e:
        log.error("HubSpot update error: %s", e)
    return None


def add_note(client: hubspot.Client, contact_id: str, body: str) -> None:
    """Creates a HubSpot NOTE and associates it with the contact."""
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    props = {
        "hs_note_body": body,
        "hs_timestamp": str(timestamp_ms),
    }
    note_input = NoteCreate(
        properties=props,
        associations=[
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    )
    try:
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note_input
        )
    except ObjectApiException as e:
        log.error("HubSpot note error: %s", e)


# ── Sync logic ───────────────────────────────────────────────────────────────

def build_contact_props(email: str, firstname: str, lastname: str, existing: dict | None) -> dict:
    domain = domain_from_email(email)
    company_guess = company_from_domain(domain)
    props: dict = {}

    existing_props = existing.properties if existing else {}

    if firstname and not existing_props.get("firstname"):
        props["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        props["lastname"] = lastname
    if company_guess and not existing_props.get("company"):
        props["company"] = company_guess
    if not existing_props.get("hs_lead_source"):
        props["hs_lead_source"] = CONTACT_SOURCE

    if not existing:
        props["email"] = email

    return props


def process_message(
    message: dict,
    state: dict,
    gmail_svc,
    hs_client: hubspot.Client,
) -> dict:
    """
    Process one Gmail message.
    Returns a result dict: {status, email, contact_id}
    """
    msg_id = message["id"]

    if msg_id in state["processed_message_ids"]:
        return {"status": "Ignorato", "email": "—", "contact_id": "—", "msg_id": msg_id}

    raw = get_message(gmail_svc, msg_id)
    headers = {h["name"]: h["value"] for h in raw.get("payload", {}).get("headers", [])}

    from_header = headers.get("From", "")
    subject = headers.get("Subject", "(no subject)")
    date_str = headers.get("Date", "")

    email, firstname, lastname = parse_sender(from_header)

    if not email or email in OWN_EMAILS:
        state["processed_message_ids"].append(msg_id)
        return {"status": "Ignorato", "email": email or "—", "contact_id": "—", "msg_id": msg_id}

    existing = find_contact_by_email(hs_client, email)
    props = build_contact_props(email, firstname, lastname, existing)

    if existing:
        contact_id = str(existing.id)
        if props:
            update_contact(hs_client, contact_id, props)
        status = "Aggiornato"
    else:
        if not props.get("email"):
            props["email"] = email
        result = create_contact(hs_client, props)
        contact_id = str(result.id) if result else "errore"
        status = "Creato" if result else "Errore"

    # Timeline note
    note_body = (
        f"[{CONTACT_TAG}] Email ricevuta il {date_str}\n"
        f"Oggetto: {subject}\n"
        f"Da: {from_header}"
    )
    add_note(hs_client, contact_id, note_body)

    state["processed_message_ids"].append(msg_id)
    return {"status": status, "email": email, "contact_id": contact_id, "msg_id": msg_id}


def print_result(r: dict) -> None:
    if r["status"] == "Ignorato":
        return
    print(
        f"  [{r['status']:10s}]  {r['email']:<45s}  ID: {r['contact_id']}"
    )


# ── Main loop ────────────────────────────────────────────────────────────────

def run_once(gmail_svc, hs_client: hubspot.Client, state: dict) -> list[dict]:
    messages = list_messages(gmail_svc, query=GMAIL_QUERY)
    results = []
    for msg in messages:
        r = process_message(msg, state, gmail_svc, hs_client)
        results.append(r)
        print_result(r)
    save_state(state)
    return results


def main():
    log.info("Gmail → HubSpot Sync avviato")
    log.info("Query Gmail: %s | Intervallo: %ds", GMAIL_QUERY, POLL_INTERVAL_SECONDS)

    gmail_svc = get_gmail_service()
    hs_client = get_hubspot_client()
    state = load_state()

    while True:
        log.info("── Ciclo di controllo (%s) ──", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        results = run_once(gmail_svc, hs_client, state)
        new_count = sum(1 for r in results if r["status"] == "Creato")
        upd_count = sum(1 for r in results if r["status"] == "Aggiornato")
        log.info("Creati: %d | Aggiornati: %d | Prossimo controllo in %ds",
                 new_count, upd_count, POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
