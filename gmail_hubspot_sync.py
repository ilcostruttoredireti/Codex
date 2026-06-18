"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails, extracts sender info, and syncs contacts to HubSpot.
Avoids duplicates using email as unique key; updates existing contacts with missing fields.
"""

import os
import re
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# -- Config ------------------------------------------------------------------
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
GMAIL_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_QUERY = os.environ.get("GMAIL_QUERY", "in:inbox -from:me newer_than:1d")
GMAIL_MAX_RESULTS = int(os.environ.get("GMAIL_MAX_RESULTS", "50"))

HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")

# Addresses to skip (own accounts, automated senders)
SKIP_ADDRESSES = {
    "noreply@", "no-reply@", "mailer-daemon@", "postmaster@",
    "facebookmail.com", "notifications@", "bounce@",
}

CONTACT_SOURCE_LABEL = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# -- Gmail -------------------------------------------------------------------

def _gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
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


def _extract_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_name_from_display(display: str) -> tuple[str, str]:
    """Return (firstname, lastname) from a display name like 'Mario Rossi'."""
    parts = display.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_from_email(email: str) -> str:
    """Extract company-friendly domain, e.g. 'acme.com' → 'Acme'."""
    try:
        domain = email.split("@")[1]
        # strip TLD suffixes and common subdomains
        for strip in ("www.", "mail.", "info.", "news."):
            domain = domain.replace(strip, "")
        name = domain.split(".")[0]
        return name.replace("-", " ").replace("_", " ").title()
    except (IndexError, AttributeError):
        return ""


def _should_skip(email: str) -> bool:
    email_lower = email.lower()
    return any(skip in email_lower for skip in SKIP_ADDRESSES)


def fetch_senders(service, query: str = GMAIL_QUERY) -> list[dict]:
    """Return unique senders from Gmail threads matching *query*."""
    result = (
        service.users()
        .threads()
        .list(userId="me", q=query, maxResults=GMAIL_MAX_RESULTS)
        .execute()
    )
    threads = result.get("threads", [])
    seen: set[str] = set()
    senders: list[dict] = []

    for thread_meta in threads:
        thread = (
            service.users()
            .threads()
            .get(userId="me", threadId=thread_meta["id"], format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        for msg in thread.get("messages", []):
            headers = msg.get("payload", {}).get("headers", [])
            label_ids = msg.get("labelIds", [])
            # Only process messages that arrived in INBOX (not sent by us)
            if "INBOX" not in label_ids:
                continue

            raw_from = _extract_header(headers, "From")
            subject = _extract_header(headers, "Subject")
            date_str = _extract_header(headers, "Date")

            display_name, email_addr = parseaddr(raw_from)
            email_addr = email_addr.lower().strip()

            if not email_addr or email_addr in seen or _should_skip(email_addr):
                continue

            seen.add(email_addr)
            firstname, lastname = _parse_name_from_display(display_name)
            company = _domain_from_email(email_addr) if not display_name else ""

            senders.append({
                "email": email_addr,
                "firstname": firstname,
                "lastname": lastname,
                "company": company,
                "subject": subject,
                "date": date_str,
            })

    log.info("Found %d unique senders in Gmail", len(senders))
    return senders


# -- HubSpot -----------------------------------------------------------------

def _hubspot_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def search_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """Return the first HubSpot contact matching *email*, or None."""
    filter_ = Filter(property_name="email", operator="EQ", value=email)
    filter_group = FilterGroup(filters=[filter_])
    req = PublicObjectSearchRequest(
        filter_groups=[filter_group],
        properties=["email", "firstname", "lastname", "company",
                    "hs_analytics_source", "hs_analytics_source_data_1"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.total > 0:
        return resp.results[0].to_dict()
    return None


def _build_properties(sender: dict, existing: Optional[dict] = None) -> dict:
    """Build the HubSpot property dict for create or partial update."""
    props: dict[str, str] = {}

    existing_props = (existing or {}).get("properties", {})

    def _set_if_missing(key: str, value: str):
        if value and not existing_props.get(key):
            props[key] = value

    _set_if_missing("email", sender["email"])
    _set_if_missing("firstname", sender["firstname"])
    _set_if_missing("lastname", sender["lastname"])
    _set_if_missing("company", sender["company"])
    # Mark source as Gmail if not already set
    _set_if_missing("hs_analytics_source", "EMAIL_MARKETING")
    _set_if_missing("hs_analytics_source_data_1", CONTACT_SOURCE_LABEL)

    return props


def create_contact(client: hubspot.Client, sender: dict) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (status, contact_id)."""
    props = _build_properties(sender)
    props["email"] = sender["email"]  # always required

    try:
        resp = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return "Creato", str(resp.id)
    except ApiException as exc:
        if exc.status == 409:
            # Already exists (race condition) — treat as existing
            log.warning("Contact %s already exists (409), treating as existing", sender["email"])
            return "Ignorato", ""
        raise


def update_contact(client: hubspot.Client, contact_id: str, sender: dict,
                   existing: dict) -> tuple[str, str]:
    """Update an existing HubSpot contact with any missing fields.
    Returns (status, contact_id).
    """
    props = _build_properties(sender, existing)
    if not props:
        return "Ignorato", contact_id

    from hubspot.crm.contacts import SimplePublicObjectInput
    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=props),
    )
    return "Aggiornato", contact_id


def add_note(client: hubspot.Client, contact_id: str, sender: dict):
    """Log a Gmail inbound note on the contact."""
    from hubspot.crm.objects.notes import (
        SimplePublicObjectInputForCreate as NoteCreate,
    )
    body = (
        f"Email inbound ricevuta da Gmail\n"
        f"Mittente: {sender['email']}\n"
        f"Oggetto: {sender.get('subject', '')}\n"
        f"Data: {sender.get('date', '')}\n"
        f"Tag: {CONTACT_TAG}"
    )
    note_props = {
        "hs_note_body": body,
        "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
    }
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(properties=note_props)
        )
        # Associate note → contact
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:
        log.warning("Could not create note for %s: %s", contact_id, exc)


# -- Main sync loop ----------------------------------------------------------

def sync():
    log.info("=== Gmail → HubSpot sync started at %s ===", datetime.now().isoformat())

    gmail = _gmail_service()
    hubspot_client = _hubspot_client()

    senders = fetch_senders(gmail)
    results: list[dict] = []

    for sender in senders:
        email = sender["email"]
        log.info("Processing: %s", email)

        existing = search_contact_by_email(hubspot_client, email)

        if existing:
            contact_id = str(existing["id"])
            status, cid = update_contact(hubspot_client, contact_id, sender, existing)
        else:
            status, cid = create_contact(hubspot_client, sender)

        if status != "Ignorato" and cid:
            add_note(hubspot_client, cid, sender)

        results.append({"status": status, "email": email, "id": cid or contact_id})
        log.info("  → %s (ID: %s)", status, cid or contact_id)

    # Summary
    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    log.info(
        "=== Sync completato: %d Creati, %d Aggiornati, %d Ignorati ===",
        counts["Creato"], counts["Aggiornato"], counts["Ignorato"],
    )

    return results


if __name__ == "__main__":
    sync()
