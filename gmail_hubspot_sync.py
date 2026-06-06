#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously, extracts sender contacts, syncs to HubSpot.
Tracks processed messages via Gmail labels — no local state file needed.
"""

import os
import re
import time
import logging
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

from dotenv import load_dotenv

# Gmail API
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# HubSpot API
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.models import (
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MAX_MESSAGES_PER_CYCLE = int(os.getenv("MAX_MESSAGES_PER_CYCLE", "50"))
SYNCED_LABEL_NAME = "HubSpot-Synced"

# Domains treated as personal (not used for company inference)
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "protonmail.com", "proton.me",
    "tutanota.com", "tutanota.de", "zohomail.com",
}

# Local parts that indicate automated senders
AUTOMATED_PREFIXES = (
    "noreply", "no-reply", "do-not-reply", "donotreply",
    "mailer-daemon", "bounce", "bounces", "notifications",
    "notification", "alerts", "alert", "info", "support",
    "newsletter", "newsletters", "unsubscribe", "postmaster",
)


# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""


@dataclass
class SyncResult:
    status: str        # Created | Updated | Ignored | Error
    email: str
    contact_id: Optional[str] = None
    reason: str = ""


# ── Contact parser ───────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> Optional[SenderContact]:
    """Extract a SenderContact from a raw From: header value."""
    display_name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email_addr):
        return None

    local_part, domain = email_addr.split("@", 1)

    if any(local_part == p or local_part.startswith(p + "+") for p in AUTOMATED_PREFIXES):
        return None

    # Parse first / last name from display name
    first_name = last_name = ""
    clean_name = re.sub(r"[\"']", "", display_name).strip()
    if clean_name:
        parts = clean_name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    # Derive company from domain (strip TLD and www)
    company = ""
    if domain not in PERSONAL_DOMAINS:
        root = domain.lstrip("www.").split(".")[0]
        company = root.capitalize() if root else ""

    return SenderContact(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


# ── Gmail client ─────────────────────────────────────────────────────────────

class GmailClient:
    def __init__(self, credentials_file: str = GMAIL_CREDENTIALS_FILE,
                 token_file: str = GMAIL_TOKEN_FILE):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._build_service()
        self._synced_label_id: Optional[str] = None

    def _build_service(self):
        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ── Label management ──────────────────────────────────────────────────────

    @property
    def synced_label_id(self) -> str:
        if self._synced_label_id is None:
            self._synced_label_id = self._get_or_create_label(SYNCED_LABEL_NAME)
        return self._synced_label_id

    def _get_or_create_label(self, name: str) -> str:
        labels = self.service.users().labels().list(userId="me").execute()
        for lbl in labels.get("labels", []):
            if lbl["name"] == name:
                return lbl["id"]

        created = self.service.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
                "color": {"backgroundColor": "#16a766", "textColor": "#ffffff"},
            },
        ).execute()
        logger.info(f"Created Gmail label '{name}' (id={created['id']})")
        return created["id"]

    # ── Message access ────────────────────────────────────────────────────────

    def fetch_unsynced_messages(self) -> list[dict]:
        """Return inbox messages that don't yet carry the synced label."""
        query = f"-label:{SYNCED_LABEL_NAME} in:inbox -in:sent"
        try:
            result = self.service.users().messages().list(
                userId="me", q=query, maxResults=MAX_MESSAGES_PER_CYCLE
            ).execute()
            return result.get("messages", [])
        except HttpError as exc:
            logger.error(f"Gmail list error: {exc}")
            return []

    def get_from_header(self, msg_id: str) -> str:
        """Fetch only the From header of a message (minimal API payload)."""
        try:
            msg = self.service.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["From"],
            ).execute()
            for header in msg.get("payload", {}).get("headers", []):
                if header["name"] == "From":
                    return header["value"]
        except HttpError as exc:
            logger.error(f"Gmail get error for {msg_id}: {exc}")
        return ""

    def mark_synced(self, msg_id: str):
        """Apply the HubSpot-Synced label to a message."""
        try:
            self.service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"addLabelIds": [self.synced_label_id]},
            ).execute()
        except HttpError as exc:
            logger.warning(f"Could not label message {msg_id}: {exc}")


# ── HubSpot client ───────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, access_token: str = HUBSPOT_ACCESS_TOKEN):
        self.client = hubspot.Client.create(access_token=access_token)

    def find_by_email(self, email: str) -> Optional[object]:
        """Return the HubSpot contact object or None."""
        search = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(filters=[
                    Filter(property_name="email", operator="EQ", value=email)
                ])
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
            limit=1,
        )
        try:
            result = self.client.crm.contacts.search_api.do_search(search)
            return result.results[0] if result.total > 0 else None
        except ApiException as exc:
            logger.error(f"HubSpot search error for {email}: {exc.body}")
            raise

    def create(self, contact: SenderContact) -> str:
        """Create a new contact and return its HubSpot ID."""
        props = {
            "email": contact.email,
            "hs_lead_source": "Gmail",
        }
        if contact.first_name:
            props["firstname"] = contact.first_name
        if contact.last_name:
            props["lastname"] = contact.last_name
        if contact.company:
            props["company"] = contact.company

        obj = self.client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return obj.id

    def update_missing_fields(self, contact_id: str, contact: SenderContact,
                              existing_props: dict) -> bool:
        """Fill only fields that are currently empty. Returns True if anything was patched."""
        updates = {}
        if contact.first_name and not existing_props.get("firstname"):
            updates["firstname"] = contact.first_name
        if contact.last_name and not existing_props.get("lastname"):
            updates["lastname"] = contact.last_name
        if contact.company and not existing_props.get("company"):
            updates["company"] = contact.company

        if not updates:
            return False

        self.client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True


# ── Sync engine ──────────────────────────────────────────────────────────────

class SyncEngine:
    def __init__(self, gmail: GmailClient, hs: HubSpotClient):
        self.gmail = gmail
        self.hs = hs

    def process_message(self, msg_id: str) -> SyncResult:
        from_header = self.gmail.get_from_header(msg_id)

        contact = parse_sender(from_header)
        if not contact:
            self.gmail.mark_synced(msg_id)
            return SyncResult(status="Ignored", email=from_header or msg_id,
                              reason="automated or invalid sender")

        try:
            existing = self.hs.find_by_email(contact.email)

            if existing is None:
                contact_id = self.hs.create(contact)
                self.gmail.mark_synced(msg_id)
                return SyncResult(status="Created", email=contact.email, contact_id=contact_id)

            updated = self.hs.update_missing_fields(
                existing.id, contact, existing.properties
            )
            self.gmail.mark_synced(msg_id)
            return SyncResult(
                status="Updated" if updated else "Ignored",
                email=contact.email,
                contact_id=existing.id,
                reason="" if updated else "contact already complete",
            )

        except Exception as exc:
            logger.error(f"Error processing {contact.email}: {exc}")
            return SyncResult(status="Error", email=contact.email, reason=str(exc))

    def run_once(self) -> list[SyncResult]:
        messages = self.gmail.fetch_unsynced_messages()
        if not messages:
            return []

        logger.info(f"Found {len(messages)} unsynced message(s).")
        results = []
        for msg in messages:
            result = self.process_message(msg["id"])
            icon = {"Created": "✚", "Updated": "↻", "Ignored": "–", "Error": "✗"}.get(
                result.status, "?"
            )
            logger.info(
                f"  {icon} [{result.status:<8}]  {result.email:<40}  "
                f"HubSpot ID: {result.contact_id or 'n/a'}"
                + (f"  ({result.reason})" if result.reason else "")
            )
            results.append(result)
        return results

    def run_continuous(self):
        logger.info(f"Gmail → HubSpot sync started  (poll every {POLL_INTERVAL}s)")
        while True:
            try:
                results = self.run_once()
                if results:
                    c = sum(1 for r in results if r.status == "Created")
                    u = sum(1 for r in results if r.status == "Updated")
                    i = sum(1 for r in results if r.status == "Ignored")
                    e = sum(1 for r in results if r.status == "Error")
                    logger.info(f"Cycle done — Created:{c}  Updated:{u}  Ignored:{i}  Error:{e}")
            except Exception as exc:
                logger.error(f"Sync cycle error: {exc}")
            time.sleep(POLL_INTERVAL)


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit("ERROR: HUBSPOT_ACCESS_TOKEN is not set. Check your .env file.")
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        raise SystemExit(
            f"ERROR: Gmail credentials not found at '{GMAIL_CREDENTIALS_FILE}'.\n"
            "Download credentials.json from Google Cloud Console → APIs & Services → Credentials."
        )

    gmail = GmailClient()
    hs = HubSpotClient()
    engine = SyncEngine(gmail, hs)
    engine.run_continuous()


if __name__ == "__main__":
    main()
