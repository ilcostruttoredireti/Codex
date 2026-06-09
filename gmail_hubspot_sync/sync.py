"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously, extracts senders, and upserts contacts in HubSpot.
"""

import os
import re
import time
import json
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

# ── Config ──────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = "token.json"
GMAIL_CREDENTIALS_FILE = "credentials.json"

HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = "last_history_id.txt"

SYSTEM_DOMAINS = {
    "googlemail.com", "google.com", "accounts.google.com",
    "legalmail.it", "postacert.istruzione.it",
}
SKIP_SENDERS = {"mailer-daemon", "no-reply", "noreply", "postmaster"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail Auth ───────────────────────────────────────────────────────────────

def get_gmail_service():
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
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ── HubSpot Client ───────────────────────────────────────────────────────────

def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _is_system(email: str) -> bool:
    local, domain = (email.lower().split("@") + [""])[:2]
    return domain in SYSTEM_DOMAINS or any(s in local for s in SKIP_SENDERS)


def parse_sender(raw_from: str) -> dict | None:
    """Parse 'From' header into {email, firstname, lastname, company}."""
    display, email = parseaddr(raw_from)
    if not email or "@" not in email or _is_system(email):
        return None

    parts = display.strip().replace('"', "").split()
    firstname = parts[0] if parts else ""
    lastname = " ".join(parts[1:]) if len(parts) > 1 else ""

    domain = email.split("@")[1]
    company = _domain_to_company(domain)

    return {
        "email": email.lower(),
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def _domain_to_company(domain: str) -> str:
    """Best-effort company name from domain (strip TLD/subdomains)."""
    skip = {"gmail", "yahoo", "hotmail", "outlook", "icloud", "libero", "virgilio"}
    parts = domain.rstrip(".").split(".")
    core = parts[-2] if len(parts) >= 2 else parts[0]
    if core.lower() in skip:
        return ""
    return core.replace("-", " ").replace("_", " ").title()


# ── HubSpot Upsert ───────────────────────────────────────────────────────────

def find_contact(client, email: str) -> dict | None:
    flt = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[flt])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0] if resp.results else None


def upsert_contact(client, sender: dict) -> tuple[str, str]:
    """
    Returns (status, hubspot_contact_id).
    status = "created" | "updated" | "skipped"
    """
    existing = find_contact(client, sender["email"])

    props = {}

    if not existing:
        props["email"] = sender["email"]
        if sender["firstname"]:
            props["firstname"] = sender["firstname"]
        if sender["lastname"]:
            props["lastname"] = sender["lastname"]
        if sender["company"]:
            props["company"] = sender["company"]
        props["hs_analytics_source"] = "EMAIL_MARKETING"  # Gmail inbound

        obj = SimplePublicObjectInputForCreate(properties=props)
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        _add_inbound_note(client, result.id, sender["email"])
        return "created", result.id

    # Patch only missing / empty fields
    existing_props = existing.properties
    if not existing_props.get("firstname") and sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if not existing_props.get("lastname") and sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if not existing_props.get("company") and sender["company"]:
        props["company"] = sender["company"]
    if not existing_props.get("hs_analytics_source"):
        props["hs_analytics_source"] = "EMAIL_MARKETING"

    if props:
        from hubspot.crm.contacts import SimplePublicObjectInput
        client.crm.contacts.basic_api.update(
            contact_id=existing.id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return "updated", existing.id

    return "skipped", existing.id


def _add_inbound_note(client, contact_id: str, email: str):
    """Attach a note to the new contact logging the inbound Gmail event."""
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput
    note_body = (
        f"Inbound Gmail\n"
        f"Contatto rilevato da email in arrivo: {email}\n"
        f"Data: {datetime.now(timezone.utc).isoformat()}\n"
        f"Tag: Inbound Gmail"
    )
    note = NoteInput(
        properties={
            "hs_note_body": note_body,
            "hs_timestamp": str(int(time.time() * 1000)),
        }
    )
    try:
        created = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note
        )
        client.crm.associations.v4.basic_api.create(
            object_type="contacts",
            object_id=contact_id,
            to_object_type="notes",
            to_object_id=created.id,
            association_spec=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        )
    except Exception as exc:
        log.warning("Could not attach note to contact %s: %s", contact_id, exc)


# ── Gmail Polling ─────────────────────────────────────────────────────────────

def load_last_history_id() -> str | None:
    if os.path.exists(STATE_FILE):
        return open(STATE_FILE).read().strip() or None
    return None


def save_history_id(history_id: str):
    with open(STATE_FILE, "w") as f:
        f.write(history_id)


def fetch_new_messages(gmail, last_history_id: str | None) -> tuple[list[dict], str]:
    """
    Returns (new_messages, current_history_id).
    First run fetches messages from the past 7 days.
    """
    profile = gmail.users().getProfile(userId="me").execute()
    current_history_id = profile["historyId"]

    if not last_history_id:
        # Bootstrap: pull recent inbox messages
        result = gmail.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=200,
        ).execute()
        messages = []
        for item in result.get("messages", []):
            msg = gmail.users().messages().get(
                userId="me", id=item["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            messages.append(msg)
        return messages, current_history_id

    # Incremental: use history API
    messages = []
    try:
        history = gmail.users().history().list(
            userId="me",
            startHistoryId=last_history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()
        for record in history.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = gmail.users().messages().get(
                    userId="me",
                    id=added["message"]["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                messages.append(msg)
    except Exception as exc:
        log.warning("History API error (will reset): %s", exc)

    return messages, current_history_id


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    gmail = get_gmail_service()
    hs = get_hubspot_client()

    log.info("Starting Gmail → HubSpot sync (poll every %ds)", POLL_INTERVAL_SECONDS)

    while True:
        last_history_id = load_last_history_id()
        try:
            messages, current_history_id = fetch_new_messages(gmail, last_history_id)
            log.info("Fetched %d new message(s)", len(messages))

            seen_emails: set[str] = set()
            for msg in messages:
                raw_from = _header(msg, "From")
                sender = parse_sender(raw_from)
                if not sender or sender["email"] in seen_emails:
                    continue
                seen_emails.add(sender["email"])

                try:
                    status, hs_id = upsert_contact(hs, sender)
                    log.info(
                        "%-8s | %-45s | HubSpot ID: %s",
                        status.upper(),
                        sender["email"],
                        hs_id,
                    )
                except ApiException as exc:
                    log.error("HubSpot error for %s: %s", sender["email"], exc)

            save_history_id(current_history_id)

        except Exception as exc:
            log.exception("Sync cycle error: %s", exc)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
