#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors incoming Gmail emails and syncs sender contacts to HubSpot,
avoiding duplicates and updating existing records with missing data.
"""

import os
import json
import time
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from email.utils import parseaddr

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import hubspot
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
)
from hubspot.crm.contacts import ApiException

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))          # seconds between cycles
LOOKBACK_QUERY = os.getenv("GMAIL_LOOKBACK", "newer_than:1d")  # Gmail search window
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "50"))

STATE_FILE = os.getenv("STATE_FILE", ".processed_emails.json")

# Addresses that should never become CRM contacts
SKIP_PATTERNS = re.compile(
    r"(no.?reply|noreply|donotreply|mailer-daemon|postmaster|bounce"
    r"|newsletter|notifications?@|unsubscribe|auto-?reply)",
    re.IGNORECASE,
)

# Free / consumer email providers — domain won't reveal company name
FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "live.it", "msn.com", "aol.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it", "tin.it",
    "fastwebnet.it", "email.it", "me.com",
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Helper utilities ──────────────────────────────────────────────────────────

def company_from_domain(email: str) -> str:
    """Derive a readable company name from the email domain."""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if not domain or domain in FREE_PROVIDERS:
        return ""
    # "acme-corp.com" → "Acme Corp"
    name = domain.split(".")[0].replace("-", " ").replace("_", " ").title()
    return name


def parse_sender(from_header: str) -> dict:
    """Return a dict with email, first_name, last_name, domain, company."""
    display_name, email = parseaddr(from_header)
    if not email:
        return {}

    email = email.lower().strip()
    first_name = last_name = ""

    if display_name:
        parts = display_name.strip().split(None, 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    domain = email.split("@")[-1] if "@" in email else ""

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company_from_domain(email),
    }


# ── Main class ────────────────────────────────────────────────────────────────

class GmailHubSpotSync:

    def __init__(self):
        self.gmail = None        # Gmail API resource
        self.hs = None           # HubSpot client
        self.processed: set = self._load_state()

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self) -> set:
        if Path(STATE_FILE).exists():
            with open(STATE_FILE) as f:
                return set(json.load(f).get("processed_ids", []))
        return set()

    def _save_state(self):
        with open(STATE_FILE, "w") as f:
            json.dump({"processed_ids": list(self.processed)}, f)

    # ── Authentication ────────────────────────────────────────────────────────

    def _auth_gmail(self):
        creds = None
        token_path = Path(GMAIL_TOKEN_FILE)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json())

        self.gmail = build("gmail", "v1", credentials=creds)
        log.info("Gmail: authenticated successfully")

    def _auth_hubspot(self):
        if not HUBSPOT_ACCESS_TOKEN:
            raise RuntimeError("HUBSPOT_ACCESS_TOKEN is not set — check your .env file")
        self.hs = hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)
        log.info("HubSpot: authenticated successfully")

    # ── Gmail helpers ─────────────────────────────────────────────────────────

    def _fetch_new_messages(self) -> list[dict]:
        """Return Gmail message stubs not yet processed."""
        try:
            resp = self.gmail.users().messages().list(
                userId="me",
                labelIds=["INBOX"],
                q=LOOKBACK_QUERY,
                maxResults=MAX_RESULTS,
            ).execute()
        except HttpError as exc:
            log.error("Gmail list error: %s", exc)
            return []

        all_msgs = resp.get("messages", [])
        return [m for m in all_msgs if m["id"] not in self.processed]

    def _get_sender_info(self, message_id: str) -> dict:
        """Fetch minimal headers for a Gmail message and return parsed sender."""
        try:
            msg = self.gmail.users().messages().get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except HttpError as exc:
            log.error("Gmail get error (%s): %s", message_id, exc)
            return {}

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        sender = parse_sender(headers.get("From", ""))
        sender["subject"] = headers.get("Subject", "")
        sender["date"] = headers.get("Date", "")
        sender["message_id"] = message_id
        return sender

    # ── HubSpot helpers ───────────────────────────────────────────────────────

    def _hs_find_contact(self, email: str):
        """Return the first HubSpot contact matching *email*, or None."""
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[Filter(property_name="email", operator="EQ", value=email)]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "leadsource"],
            limit=1,
        )
        try:
            result = self.hs.crm.contacts.search_api.do_search(
                public_object_search_request=search_req
            )
            return result.results[0] if result.results else None
        except ApiException as exc:
            log.error("HubSpot search error (%s): %s", email, exc)
            return None

    def _hs_create_contact(self, sender: dict):
        """Create a new HubSpot contact and return the created object."""
        props = {
            "email": sender["email"],
            "leadsource": "Gmail",
        }
        if sender.get("first_name"):
            props["firstname"] = sender["first_name"]
        if sender.get("last_name"):
            props["lastname"] = sender["last_name"]
        if sender.get("company"):
            props["company"] = sender["company"]

        try:
            return self.hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
        except ApiException as exc:
            log.error("HubSpot create error (%s): %s", sender["email"], exc)
            return None

    def _hs_update_contact(self, contact_id: str, sender: dict, existing) -> bool:
        """
        Patch only the fields that are currently blank in HubSpot.
        Returns True if at least one field was patched.
        """
        ep = existing.properties or {}
        updates = {}

        if not ep.get("firstname") and sender.get("first_name"):
            updates["firstname"] = sender["first_name"]
        if not ep.get("lastname") and sender.get("last_name"):
            updates["lastname"] = sender["last_name"]
        if not ep.get("company") and sender.get("company"):
            updates["company"] = sender["company"]
        if not ep.get("leadsource"):
            updates["leadsource"] = "Gmail"

        if not updates:
            return False

        try:
            self.hs.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            return True
        except ApiException as exc:
            log.error("HubSpot update error (id=%s): %s", contact_id, exc)
            return False

    def _hs_add_note(self, contact_id: str, sender: dict):
        """Create an email-received note on the contact's timeline."""
        body = (
            f"Email inbound ricevuta da {sender.get('email', '')}.\n"
            f"Oggetto: {sender.get('subject', 'N/A')}\n"
            f"Data: {sender.get('date', 'N/A')}\n"
            f"Tag: Inbound Gmail"
        )
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        note_props = {
            "hs_note_body": body,
            "hs_timestamp": str(timestamp_ms),
        }

        try:
            note = self.hs.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=note_props,
                    associations=[
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 202,  # note → contact
                                }
                            ],
                        }
                    ],
                )
            )
            log.debug("Note created (id=%s) for contact %s", note.id, contact_id)
        except Exception as exc:
            # Notes are optional — log but don't fail the whole record
            log.warning("Could not create note for contact %s: %s", contact_id, exc)

    # ── Core processing ───────────────────────────────────────────────────────

    def _process_message(self, message_id: str) -> dict:
        """
        Process a single Gmail message.

        Returns a result dict:
            status  : "Creato" | "Aggiornato" | "Ignorato" | "Errore"
            email   : sender email address
            id      : HubSpot contact ID (empty when Ignorato/Errore)
        """
        result = {"status": "Ignorato", "email": "", "id": ""}

        sender = self._get_sender_info(message_id)
        if not sender or not sender.get("email"):
            self.processed.add(message_id)
            return result

        email = sender["email"]
        result["email"] = email

        if SKIP_PATTERNS.search(email):
            log.debug("Skipping automated address: %s", email)
            self.processed.add(message_id)
            return result

        existing = self._hs_find_contact(email)

        if existing:
            contact_id = existing.id
            result["id"] = contact_id
            patched = self._hs_update_contact(contact_id, sender, existing)
            result["status"] = "Aggiornato" if patched else "Ignorato"
            self._hs_add_note(contact_id, sender)
            log.info(
                "[%s] %s  (HubSpot id: %s)",
                result["status"], email, contact_id
            )
        else:
            created = self._hs_create_contact(sender)
            if created:
                result["id"] = created.id
                result["status"] = "Creato"
                self._hs_add_note(created.id, sender)
                log.info("[Creato] %s  (HubSpot id: %s)", email, created.id)
            else:
                result["status"] = "Errore"
                log.error("[Errore] Could not create contact for %s", email)

        self.processed.add(message_id)
        return result

    # ── Public API ────────────────────────────────────────────────────────────

    def run_once(self) -> list[dict]:
        """Execute a single sync pass and return the list of result dicts."""
        new_msgs = self._fetch_new_messages()
        if not new_msgs:
            log.info("No new emails to process.")
            return []

        log.info("Processing %d new message(s)...", len(new_msgs))
        results = [self._process_message(m["id"]) for m in new_msgs]
        self._save_state()

        created = sum(1 for r in results if r["status"] == "Creato")
        updated = sum(1 for r in results if r["status"] == "Aggiornato")
        ignored = sum(1 for r in results if r["status"] == "Ignorato")
        errors  = sum(1 for r in results if r["status"] == "Errore")

        log.info(
            "Cycle done ─ Creati: %d  Aggiornati: %d  Ignorati: %d  Errori: %d",
            created, updated, ignored, errors,
        )
        return results

    def run(self):
        """Authenticate once, then poll Gmail in a continuous loop."""
        log.info("═══ Gmail → HubSpot Sync starting ═══")
        self._auth_gmail()
        self._auth_hubspot()

        while True:
            try:
                results = self.run_once()
                # Print tabular summary to stdout
                if results:
                    print()
                    print(f"{'Stato':<12} {'Email':<40} {'HubSpot ID'}")
                    print("─" * 70)
                    for r in results:
                        print(f"{r['status']:<12} {r['email']:<40} {r['id']}")
                    print()
            except KeyboardInterrupt:
                log.info("Interrupted by user — exiting.")
                break
            except Exception as exc:
                log.exception("Unexpected error in sync cycle: %s", exc)

            log.info("Next check in %ds...", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    GmailHubSpotSync().run()
