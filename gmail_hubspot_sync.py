"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts sender contacts, and syncs them to HubSpot.
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

import httpx
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from hubspot import HubSpot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))

# Domains to ignore (notifications, own account, etc.)
IGNORED_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "accounts.google.com",
    "mailer-daemon.googlemail.com",
}

# Sender emails to skip (the inbox itself, etc.)
IGNORED_EMAILS = set(
    e.strip()
    for e in os.getenv("IGNORED_EMAILS", "").split(",")
    if e.strip()
)


# ── data model ──────────────────────────────────────────────────────────────

@dataclass
class Contact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source: str = "Gmail"
    thread_id: str = ""
    subject: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str          # Creato | Aggiornato | Ignorato | Errore
    reason: str = ""


# ── Gmail helpers ────────────────────────────────────────────────────────────

_DA_PATTERN = re.compile(
    r'Da\s+"([^"]+)"\s+([\w.+\-]+@[\w.\-]+)',
    re.IGNORECASE,
)
_DA_PLAIN_PATTERN = re.compile(
    r'Da\s+([\w.+\-]+@[\w.\-]+)',
    re.IGNORECASE,
)
_FWD_PATTERN = re.compile(
    r'Da:\s+(?:"?([^"<\n]+)"?\s+)?<?([\w.+\-]+@[\w.\-]+)>?',
    re.IGNORECASE,
)


def _get_gmail_service():
    """Authenticate and return a Gmail API service client."""
    token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _parse_forwarded_sender(snippet: str) -> Optional[tuple[str, str]]:
    """
    Return (name, email) from a forwarded-message snippet,
    e.g. 'Da "Carola Assumma" carola@example.com …'
    Returns None if no match found.
    """
    m = _DA_PATTERN.search(snippet)
    if m:
        name, email = m.group(1).strip(), m.group(2).strip().lower()
        return name, email

    m = _FWD_PATTERN.search(snippet)
    if m:
        name = (m.group(1) or "").strip()
        email = m.group(2).strip().lower()
        return name, email

    m = _DA_PLAIN_PATTERN.search(snippet)
    if m:
        return "", m.group(1).strip().lower()

    return None


def _name_parts(full_name: str) -> tuple[str, str]:
    """Split 'Mario Rossi' into ('Mario', 'Rossi')."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], ""


def _company_from_domain(domain: str) -> str:
    """
    Best-effort company name from email domain.
    Strips TLD and common prefixes; capitalises.
    """
    if not domain or domain.endswith("gmail.com") or domain.endswith("yahoo.com"):
        return ""
    name = domain.split(".")[0]
    # remove common prefixes
    for prefix in ("ufficiostampa", "press", "info", "contact", "media", "redazione"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):].lstrip("-_") or name
    return name.replace("-", " ").replace("_", " ").title()


def fetch_recent_emails(service, hours: int = 24) -> list[Contact]:
    """
    Query Gmail for emails received in the last `hours` hours.
    Returns a deduplicated list of Contact objects (keyed by email).
    """
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"in:inbox after:{since} -from:me"

    results = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=100)
        .execute()
    )
    messages = results.get("messages", [])

    seen: dict[str, Contact] = {}

    for msg_stub in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_stub["id"], format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "")
        snippet = msg.get("snippet", "")
        thread_id = msg.get("threadId", "")

        # --- parse direct sender ---
        m = re.match(r'"?([^"<]+)"?\s*<?([^>@\s]+@[^>\s]+)>?', raw_from)
        if m:
            sender_name = m.group(1).strip()
            sender_email = m.group(2).strip().lower()
        else:
            addr_m = re.search(r'[\w.+\-]+@[\w.\-]+', raw_from)
            sender_email = addr_m.group(0).lower() if addr_m else ""
            sender_name = ""

        contacts_to_process = []

        # --- forwarded / press-release inbox: extract original sender ---
        is_fwd = subject.startswith(("Fw:", "Fwd:", "FW:", "FWD:"))
        if is_fwd or "Da " in snippet or "Da:" in snippet:
            parsed = _parse_forwarded_sender(snippet)
            if parsed:
                orig_name, orig_email = parsed
                if orig_email and orig_email != sender_email:
                    contacts_to_process.append((orig_name, orig_email, thread_id, subject))

        # always add the direct sender too (unless it's the fwd relay itself)
        if sender_email:
            contacts_to_process.append((sender_name, sender_email, thread_id, subject))

        for name, email, tid, subj in contacts_to_process:
            domain = email.split("@")[-1]
            if domain in IGNORED_DOMAINS:
                continue
            if email in IGNORED_EMAILS:
                continue
            if email in seen:
                continue

            firstname, lastname = _name_parts(name) if name else ("", "")
            company = _company_from_domain(domain)

            seen[email] = Contact(
                email=email,
                firstname=firstname,
                lastname=lastname,
                company=company,
                thread_id=tid,
                subject=subj,
            )

    return list(seen.values())


# ── HubSpot helpers ──────────────────────────────────────────────────────────

def _hs_client() -> HubSpot:
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return HubSpot(access_token=token)


def find_contact(client: HubSpot, email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    search = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=search)
    if resp.total > 0:
        return resp.results[0]
    return None


def _build_properties(contact: Contact, existing: Optional[dict] = None) -> dict:
    """Build HubSpot property dict, only filling in missing/empty fields."""
    existing_props = existing.properties if existing else {}

    props = {"email": contact.email}

    if contact.firstname and not existing_props.get("firstname"):
        props["firstname"] = contact.firstname
    if contact.lastname and not existing_props.get("lastname"):
        props["lastname"] = contact.lastname
    if contact.company and not existing_props.get("company"):
        props["company"] = contact.company

    # Mark source as Gmail if not already set to something meaningful
    current_source = existing_props.get("hs_analytics_source", "")
    if not current_source or current_source == "OFFLINE":
        props["hs_analytics_source"] = "EMAIL"

    return props


def create_contact(client: HubSpot, contact: Contact) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (id, status)."""
    props = {
        "email": contact.email,
        "firstname": contact.firstname,
        "lastname": contact.lastname,
        "company": contact.company,
        "hs_analytics_source": "EMAIL",
    }
    props = {k: v for k, v in props.items() if v}

    obj = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=props
        )
    )
    return obj.id, "Creato"


def update_contact(client: HubSpot, contact: Contact, existing) -> tuple[str, str]:
    """Update missing fields on an existing HubSpot contact. Returns (id, status)."""
    props = _build_properties(contact, existing)
    # Remove email from update payload (can't change primary email this way)
    props.pop("email", None)

    if not props:
        return existing.id, "Ignorato"

    from hubspot.crm.contacts import SimplePublicObjectInput

    client.crm.contacts.basic_api.update(
        contact_id=existing.id,
        simple_public_object_input=SimplePublicObjectInput(properties=props),
    )
    return existing.id, "Aggiornato"


def log_email_activity(client: HubSpot, contact_id: str, contact: Contact) -> None:
    """Attach an inbound email note to the contact timeline."""
    from hubspot.crm.objects.notes import (
        SimplePublicObjectInputForCreate as NoteInput,
    )
    note_body = (
        f"📧 Email inbound ricevuta via Gmail\n"
        f"Oggetto: {contact.subject}\n"
        f"Thread ID: {contact.thread_id}"
    )
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteInput(
                properties={
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                }
            )
        )
        client.crm.associations.v4.basic_api.create(
            object_type="notes",
            object_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_spec=[
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}
            ],
        )
    except Exception as exc:
        log.warning("Could not create note for %s: %s", contact.email, exc)


# ── main sync loop ────────────────────────────────────────────────────────────

def run_sync(
    lookback_hours: int = LOOKBACK_HOURS,
    create_notes: bool = True,
) -> list[SyncResult]:
    log.info("Starting Gmail → HubSpot sync (last %dh)", lookback_hours)
    service = _get_gmail_service()
    hs = _hs_client()

    contacts = fetch_recent_emails(service, hours=lookback_hours)
    log.info("Extracted %d unique contacts from Gmail", len(contacts))

    results: list[SyncResult] = []

    for contact in contacts:
        try:
            existing = find_contact(hs, contact.email)

            if existing is None:
                cid, status = create_contact(hs, contact)
            else:
                cid, status = update_contact(hs, contact, existing)

            if create_notes and status in ("Creato", "Aggiornato"):
                log_email_activity(hs, cid, contact)

            results.append(SyncResult(email=contact.email, hubspot_id=cid, status=status))
            log.info("[%s] %s (ID: %s)", status, contact.email, cid)

        except ApiException as exc:
            log.error("HubSpot API error for %s: %s", contact.email, exc)
            results.append(
                SyncResult(
                    email=contact.email,
                    hubspot_id=None,
                    status="Errore",
                    reason=str(exc),
                )
            )

    # Summary
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info("Done. %s", " | ".join(f"{k}: {v}" for k, v in counts.items()))

    return results


def print_report(results: list[SyncResult]) -> None:
    print(f"\n{'─'*60}")
    print(f"{'STATO':<12} {'EMAIL CONTATTO':<40} {'ID HUBSPOT'}")
    print(f"{'─'*60}")
    for r in results:
        print(f"{r.status:<12} {r.email:<40} {r.hubspot_id or '—'}")
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    results = run_sync()
    print_report(results)
