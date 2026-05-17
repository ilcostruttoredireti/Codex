#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts senders, syncs to HubSpot avoiding duplicates.
"""

import os
import re
import time
import sqlite3
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
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

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 minutes
DB_FILE = os.getenv("DB_FILE", "processed_emails.db")
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Domains to skip (automated senders)
SKIP_DOMAINS = {
    "facebookmail.com", "facebook.com", "linkedin.com", "twitter.com",
    "noreply.github.com", "notifications.google.com", "accounts.google.com",
    "bounce.mail.google.com", "mailer-daemon",
}
SKIP_EMAIL_PATTERNS = [
    r"^no[-_]?reply@",
    r"^noreply@",
    r"^notification",
    r"^bounce@",
    r"^mailer-daemon@",
    r"^postmaster@",
    r"^do[-_]not[-_]reply@",
    r"^donotreply@",
    r"^auto[-_]?reply@",
]

# Italian/English forwarded email header patterns
FWD_PATTERNS = [
    # Italian: Da "Name" email@domain.com
    r'Da\s+"([^"]+)"\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    # Italian: Da Name <email@domain.com>
    r'Da:\s+([^<\n]+?)\s+<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
    # English: From "Name" <email@domain.com>
    r'From:\s+"?([^"<\n]+?)"?\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
    # Plain email in Italian forward: Da: email@domain.com
    r'Da:\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    # Plain email only: Da email@domain.com (no name)
    r'Da\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
]


@dataclass
class Contact:
    email: str
    name: str = ""
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source: str = CONTACT_SOURCE
    thread_id: str = ""
    email_subject: str = ""
    email_date: str = ""

    def __post_init__(self):
        if self.name and not self.firstname:
            parts = self.name.strip().split(" ", 1)
            self.firstname = parts[0].capitalize()
            self.lastname = parts[1].capitalize() if len(parts) > 1 else ""
        if not self.company:
            domain = self.email.split("@")[-1].lower()
            if domain not in {"gmail.com", "yahoo.com", "hotmail.com", "libero.it",
                              "outlook.com", "icloud.com", "tiscali.it", "virgilio.it"}:
                self.company = domain


def setup_db(db_file: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_threads (
            thread_id TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL,
            contact_email TEXT,
            hubspot_id TEXT,
            status TEXT
        )
    """)
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, thread_id: str) -> bool:
    row = conn.execute(
        "SELECT thread_id FROM processed_threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    return row is not None


def mark_processed(conn: sqlite3.Connection, thread_id: str, contact_email: str,
                   hubspot_id: str, status: str):
    conn.execute(
        """INSERT OR REPLACE INTO processed_threads
           (thread_id, processed_at, contact_email, hubspot_id, status)
           VALUES (?, ?, ?, ?, ?)""",
        (thread_id, datetime.utcnow().isoformat(), contact_email, hubspot_id, status),
    )
    conn.commit()


def is_automated_sender(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    if domain in SKIP_DOMAINS:
        return True
    for pattern in SKIP_EMAIL_PATTERNS:
        if re.match(pattern, email.lower()):
            return True
    return False


def extract_forwarded_sender(snippet: str) -> tuple[str, str]:
    """Returns (email, name) from a forwarded email snippet."""
    for pattern in FWD_PATTERNS:
        match = re.search(pattern, snippet, re.IGNORECASE)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                name_or_email, email_or_none = groups
                # Determine which group is the email
                if "@" in name_or_email and "@" not in (email_or_none or ""):
                    return name_or_email.strip(), ""
                return email_or_none.strip(), name_or_email.strip()
            elif len(groups) == 1:
                return groups[0].strip(), ""
    return "", ""


def gmail_authenticate() -> object:
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
        with open(GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_threads(service, max_results: int = 50) -> list[dict]:
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
        q="-in:sent -in:draft",
    ).execute()
    return result.get("messages", [])


def get_thread_sender(service, thread_id: str) -> tuple[str, str, str, str]:
    """Returns (sender_email, sender_name, subject, date) from thread's first message."""
    thread = service.users().threads().get(userId="me", id=thread_id, format="metadata",
                                           metadataHeaders=["From", "Subject", "Date"]).execute()
    messages = thread.get("messages", [])
    if not messages:
        return "", "", "", ""
    headers = {h["name"]: h["value"] for h in messages[0].get("payload", {}).get("headers", [])}
    from_header = headers.get("From", "")
    subject = headers.get("Subject", "")
    date = headers.get("Date", "")

    # Parse "Name <email>" or just "email"
    name_match = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', from_header)
    if name_match:
        return name_match.group(2).strip(), name_match.group(1).strip(), subject, date
    email_match = re.match(r'([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', from_header)
    if email_match:
        return email_match.group(1).strip(), "", subject, date
    return from_header.strip(), "", subject, date


def get_message_snippet(service, message_id: str) -> str:
    msg = service.users().messages().get(userId="me", id=message_id, format="minimal").execute()
    return msg.get("snippet", "")


def find_hubspot_contact(hs_client, email: str) -> Optional[dict]:
    filter_obj = Filter(property_name="email", operator="EQ", value=email)
    filter_group = FilterGroup(filters=[filter_obj])
    search_req = PublicObjectSearchRequest(
        filter_groups=[filter_group],
        properties=["email", "firstname", "lastname", "company", "lifecyclestage"],
        limit=1,
    )
    try:
        response = hs_client.crm.contacts.search_api.do_search(search_req)
        if response.results:
            return response.results[0]
    except ApiException as e:
        log.error("HubSpot search error: %s", e)
    return None


def create_hubspot_contact(hs_client, contact: Contact) -> Optional[str]:
    props = {
        "email": contact.email,
        "lifecyclestage": "lead",
        "hs_analytics_source": "OTHER",
        "hs_analytics_source_data_1": CONTACT_SOURCE,
        "hs_analytics_source_data_2": CONTACT_TAG,
    }
    if contact.firstname:
        props["firstname"] = contact.firstname
    if contact.lastname:
        props["lastname"] = contact.lastname
    if contact.company:
        props["company"] = contact.company

    try:
        obj = hs_client.crm.contacts.basic_api.create(
            SimplePublicObjectInputForCreate(properties=props)
        )
        return str(obj.id)
    except ApiException as e:
        log.error("HubSpot create error for %s: %s", contact.email, e)
    return None


def update_hubspot_contact(hs_client, contact_id: str, contact: Contact,
                            existing: dict) -> bool:
    """Updates only missing fields on an existing contact."""
    existing_props = existing.properties if hasattr(existing, "properties") else existing.get("properties", {})
    updates = {}

    if not existing_props.get("firstname") and contact.firstname:
        updates["firstname"] = contact.firstname
    if not existing_props.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname
    if not existing_props.get("company") and contact.company:
        updates["company"] = contact.company

    if not updates:
        return False

    from hubspot.crm.contacts import SimplePublicObjectInput
    try:
        hs_client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as e:
        log.error("HubSpot update error for %s: %s", contact.email, e)
    return False


def process_email(service, hs_client, db_conn: sqlite3.Connection,
                  thread_id: str, message_id: str) -> dict:
    result = {"thread_id": thread_id, "email": "", "hubspot_id": "", "status": "IGNORATO"}

    if is_processed(db_conn, thread_id):
        result["status"] = "GIA_PROCESSATO"
        return result

    sender_email, sender_name, subject, date = get_thread_sender(service, thread_id)

    # For forwarded emails, try to extract the real original sender
    if sender_email.lower() in {"redazione@latestata.it"} or "Fw:" in subject or "Fwd:" in subject:
        snippet = get_message_snippet(service, message_id)
        fwd_email, fwd_name = extract_forwarded_sender(snippet)
        if fwd_email and not is_automated_sender(fwd_email):
            sender_email = fwd_email
            sender_name = fwd_name or sender_name

    if not sender_email or is_automated_sender(sender_email):
        mark_processed(db_conn, thread_id, sender_email, "", "IGNORATO")
        result["email"] = sender_email
        return result

    contact = Contact(
        email=sender_email.lower(),
        name=sender_name,
        thread_id=thread_id,
        email_subject=subject,
        email_date=date,
    )
    result["email"] = contact.email

    existing = find_hubspot_contact(hs_client, contact.email)

    if existing:
        contact_id = str(existing.id) if hasattr(existing, "id") else str(existing["id"])
        updated = update_hubspot_contact(hs_client, contact_id, contact, existing)
        status = "AGGIORNATO" if updated else "GIA_AGGIORNATO"
        result.update({"hubspot_id": contact_id, "status": status})
    else:
        contact_id = create_hubspot_contact(hs_client, contact)
        if contact_id:
            result.update({"hubspot_id": contact_id, "status": "CREATO"})
        else:
            result["status"] = "ERRORE"

    mark_processed(db_conn, thread_id, contact.email, result["hubspot_id"], result["status"])
    return result


def sync_loop():
    if not HUBSPOT_API_KEY:
        raise SystemExit("HUBSPOT_API_KEY environment variable is required.")
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        raise SystemExit(f"Gmail credentials file not found: {GMAIL_CREDENTIALS_FILE}")

    log.info("Authenticating with Gmail...")
    gmail_service = gmail_authenticate()

    log.info("Connecting to HubSpot...")
    hs_client = hubspot.Client.create(access_token=HUBSPOT_API_KEY)

    db_conn = setup_db(DB_FILE)
    log.info("Database ready: %s", DB_FILE)
    log.info("Starting sync loop — polling every %ds", POLL_INTERVAL_SECONDS)

    while True:
        log.info("--- Polling Gmail inbox ---")
        try:
            messages = fetch_inbox_threads(gmail_service, max_results=50)
            log.info("Found %d messages", len(messages))

            stats = {"CREATO": 0, "AGGIORNATO": 0, "GIA_AGGIORNATO": 0,
                     "IGNORATO": 0, "ERRORE": 0, "GIA_PROCESSATO": 0}

            for msg in messages:
                thread_id = msg["id"]
                result = process_email(gmail_service, hs_client, db_conn,
                                       thread_id, thread_id)

                status = result["status"]
                stats[status] = stats.get(status, 0) + 1

                if status not in ("GIA_PROCESSATO", "IGNORATO"):
                    log.info(
                        "%-15s | %-45s | HubSpot ID: %s",
                        status, result["email"] or "(nessuna email)", result["hubspot_id"]
                    )

            log.info(
                "Completato — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d",
                stats["CREATO"], stats["AGGIORNATO"],
                stats["IGNORATO"], stats["ERRORE"],
            )

        except Exception as e:
            log.error("Sync error: %s", e, exc_info=True)

        log.info("Prossimo polling tra %ds...", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    sync_loop()
