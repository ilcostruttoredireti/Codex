"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot.
"""

import os
import re
import json
import time
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from email import message_from_bytes
from email.header import decode_header

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput, ApiException
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup

# ── Config ──────────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
STATE_FILE = os.getenv("STATE_FILE", ".gmail_sync_state.json")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# Domains/addresses to ignore (automated senders)
IGNORED_SENDERS = {
    "mailer-daemon@googlemail.com",
    "noreply@accounts.google.com",
    "noreply@google.com",
    "no-reply@accounts.google.com",
}
IGNORED_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "linkedin.com",
    "mailchimp.com",
    "sendgrid.net",
    "amazonses.com",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail auth ───────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ── State management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"processed_message_ids": [], "last_history_id": None}


def save_state(state: dict):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ── Email parsing ─────────────────────────────────────────────────────────────

_RE_FORWARDED = re.compile(
    r'Da\s+"([^"]+)"\s+([\w.+\-]+@[\w.\-]+)',
    re.IGNORECASE,
)

def _decode_header_value(value: str) -> str:
    parts = decode_header(value)
    decoded = []
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return " ".join(decoded)


def parse_name_email(raw: str) -> tuple[str, str]:
    """Return (display_name, email) from a 'Name <email>' or bare 'email' string."""
    m = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', raw.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    m = re.match(r'([\w.+\-]+@[\w.\-]+)', raw.strip())
    if m:
        return "", m.group(1).lower()
    return "", ""


def extract_forwarded_sender(body_text: str) -> tuple[str, str] | None:
    """Detect 'Da "Nome" email@domain' pattern in forwarded emails."""
    m = _RE_FORWARDED.search(body_text)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return None


def split_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname). Best-effort."""
    parts = display_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    # Heuristic: last word is lastname for personal names
    # If name starts with "Ufficio" / "Ufficio Stampa" it's an org role
    org_keywords = {"ufficio", "ufficiostampa", "press", "comunicazione", "redazione"}
    if parts[0].lower() in org_keywords:
        return display_name, ""
    return " ".join(parts[:-1]), parts[-1]


def domain_from_email(email: str) -> str:
    return email.split("@")[-1] if "@" in email else ""


def company_from_domain(domain: str) -> str:
    """Derive a company name hint from the email domain."""
    if not domain or domain.endswith("gmail.com") or domain.endswith("libero.it"):
        return ""
    # Strip common TLDs and 'www.'
    name = re.sub(r'\.(it|com|org|net|eu|gov|edu)$', '', domain)
    name = re.sub(r'^www\.', '', name)
    return name.replace(".", " ").replace("-", " ").title()


def is_ignored(email: str) -> bool:
    if email in IGNORED_SENDERS:
        return True
    domain = domain_from_email(email)
    return any(domain.endswith(d) for d in IGNORED_DOMAINS)


def get_message_body(msg_payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = msg_payload.get("mimeType", "")
    if mime_type == "text/plain":
        data = msg_payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    if mime_type.startswith("multipart/"):
        for part in msg_payload.get("parts", []):
            text = get_message_body(part)
            if text:
                return text
    return ""


def fetch_new_messages(service, state: dict) -> list[dict]:
    """Return Gmail messages not yet processed."""
    results = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=50)
        .execute()
    )
    messages = results.get("messages", [])
    processed = set(state.get("processed_message_ids", []))
    new_messages = [m for m in messages if m["id"] not in processed]
    return new_messages


def get_message_detail(service, msg_id: str) -> dict:
    return (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="full")
        .execute()
    )


def extract_sender_info(msg_detail: dict) -> dict | None:
    """
    Returns dict with: email, firstname, lastname, company, domain
    or None if the sender should be ignored.
    """
    headers = {
        h["name"].lower(): h["value"]
        for h in msg_detail.get("payload", {}).get("headers", [])
    }
    raw_from = headers.get("from", "")
    display_name, sender_email = parse_name_email(raw_from)

    if not sender_email or is_ignored(sender_email):
        return None

    # For forwarded emails, try to find the real original sender
    body = get_message_body(msg_detail.get("payload", {}))
    fwd = extract_forwarded_sender(body)
    if fwd:
        display_name, sender_email = fwd
        if is_ignored(sender_email):
            return None

    firstname, lastname = split_name(display_name)
    domain = domain_from_email(sender_email)
    company = company_from_domain(domain)

    return {
        "email": sender_email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def find_contact_by_email(client, email: str) -> dict | None:
    """Search HubSpot for a contact by email. Returns contact dict or None."""
    api: SearchApi = client.crm.contacts.search_api
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[
                    Filter(property_name="email", operator="EQ", value=email)
                ]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        result = api.do_search(public_object_search_request=req)
        return result.results[0].to_dict() if result.results else None
    except ApiException as exc:
        log.error("HubSpot search error: %s", exc)
        return None


def create_contact(client, info: dict) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (status, contact_id)."""
    api: BasicApi = client.crm.contacts.basic_api
    props = {
        "email": info["email"],
        "leadsource": CONTACT_SOURCE,
    }
    if info.get("firstname"):
        props["firstname"] = info["firstname"]
    if info.get("lastname"):
        props["lastname"] = info["lastname"]
    if info.get("company"):
        props["company"] = info["company"]
    # HubSpot doesn't have a native "tag" field; use hs_lead_status or a notes field.
    # We store the inbound tag in a note instead (see add_note_to_contact).

    try:
        result = api.create(
            simple_public_object_input_for_create=SimplePublicObjectInput(
                properties=props
            )
        )
        return "CREATO", result.id
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", info["email"], exc)
        return "ERRORE", ""


def update_contact(client, contact_id: str, info: dict, existing: dict) -> tuple[str, str]:
    """Fill in missing fields on an existing contact."""
    api: BasicApi = client.crm.contacts.basic_api
    existing_props = existing.get("properties", {})
    updates = {}

    if not existing_props.get("firstname") and info.get("firstname"):
        updates["firstname"] = info["firstname"]
    if not existing_props.get("lastname") and info.get("lastname"):
        updates["lastname"] = info["lastname"]
    if not existing_props.get("company") and info.get("company"):
        updates["company"] = info["company"]
    if not existing_props.get("leadsource"):
        updates["leadsource"] = CONTACT_SOURCE

    if not updates:
        return "IGNORATO", contact_id

    try:
        api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return "AGGIORNATO", contact_id
    except ApiException as exc:
        log.error("HubSpot update error for %s: %s", contact_id, exc)
        return "ERRORE", contact_id


def add_engagement_note(client, contact_id: str, subject: str, sender_email: str):
    """Add an email-received note/engagement to the contact timeline."""
    try:
        note_body = (
            f"Email inbound ricevuta da: {sender_email}\n"
            f"Oggetto: {subject}\n"
            f"Fonte: {INBOUND_TAG}\n"
            f"Data: {datetime.now(timezone.utc).isoformat()}"
        )
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInput(
                properties={
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(time.time() * 1000)),
                }
            )
        )
        # Note: association to contact would require an additional API call
        # to crm.associations, left as an optional extension.
    except Exception as exc:
        log.warning("Could not add engagement note: %s", exc)


# ── Main sync loop ────────────────────────────────────────────────────────────

def process_message(service, client, msg_id: str, state: dict) -> dict:
    """Process a single Gmail message. Returns a result dict."""
    msg = get_message_detail(service, msg_id)
    headers = {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    subject = _decode_header_value(headers.get("subject", "(no subject)"))

    info = extract_sender_info(msg)
    if info is None:
        return {"status": "IGNORATO", "email": headers.get("from", ""), "id": "—"}

    existing = find_contact_by_email(client, info["email"])
    if existing:
        status, contact_id = update_contact(client, existing["id"], info, existing)
    else:
        status, contact_id = create_contact(client, info)

    if contact_id:
        add_engagement_note(client, contact_id, subject, info["email"])

    return {"status": status, "email": info["email"], "id": contact_id}


def sync_once(service, client, state: dict) -> list[dict]:
    new_msgs = fetch_new_messages(service, state)
    if not new_msgs:
        log.info("Nessun nuovo messaggio.")
        return []

    results = []
    for msg in new_msgs:
        msg_id = msg["id"]
        try:
            result = process_message(service, client, msg_id, state)
        except Exception as exc:
            log.error("Errore msg %s: %s", msg_id, exc)
            result = {"status": "ERRORE", "email": "?", "id": "—"}

        results.append(result)
        state.setdefault("processed_message_ids", []).append(msg_id)

        # Keep state list bounded
        if len(state["processed_message_ids"]) > 10_000:
            state["processed_message_ids"] = state["processed_message_ids"][-5_000:]

        log.info("[%s] %s → ID: %s", result["status"], result["email"], result["id"])

    save_state(state)
    return results


def print_report(results: list[dict]):
    print("\n" + "=" * 60)
    print(f"{'Stato':<12} {'Email':<42} {'HubSpot ID'}")
    print("-" * 60)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<42} {r['id']}")
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("-" * 60)
    for status, n in counts.items():
        print(f"  {status}: {n}")
    print("=" * 60 + "\n")


def run():
    log.info("Avvio Gmail → HubSpot sync (polling ogni %ds)", POLL_INTERVAL_SECONDS)
    service = get_gmail_service()
    client = get_hubspot_client()
    state = load_state()

    while True:
        log.info("Controllo nuove email…")
        results = sync_once(service, client, state)
        if results:
            print_report(results)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
