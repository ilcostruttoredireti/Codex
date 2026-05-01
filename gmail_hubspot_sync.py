#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and upserts sender contacts into HubSpot.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.exceptions import ApiException
from hubspot.crm.objects.notes import (
    SimplePublicObjectInputForCreate as NoteCreate,
)

# ─── Constants ────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
STATE_FILE = Path("processed_emails.json")
TOKEN_FILE = Path("gmail_token.json")
POLL_INTERVAL = 60  # seconds between inbox checks

FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "protonmail.com", "live.com", "aol.com",
    "mail.com", "gmx.com", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "tin.it", "fastwebnet.it",
}

NO_REPLY_PREFIXES = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications",
    "newsletter", "news",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Main class ───────────────────────────────────────────────────────────────

class GmailHubSpotSync:
    def __init__(self, credentials_file: str, hubspot_token: str):
        self.gmail = self._auth_gmail(credentials_file)
        self.hs = hubspot.Client.create(access_token=hubspot_token)
        self.processed: set = self._load_state()

    # ── Gmail ─────────────────────────────────────────────────────────────────

    def _auth_gmail(self, credentials_file: str):
        creds = None
        if TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(
                str(TOKEN_FILE), GMAIL_SCOPES
            )
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    credentials_file, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            TOKEN_FILE.write_text(creds.to_json())
        return build("gmail", "v1", credentials=creds)

    def _load_state(self) -> set:
        if STATE_FILE.exists():
            return set(json.loads(STATE_FILE.read_text()))
        return set()

    def _save_state(self):
        STATE_FILE.write_text(json.dumps(sorted(self.processed)))

    def fetch_new_inbox_messages(self) -> list[dict]:
        """Return unread inbox messages not yet processed."""
        try:
            resp = (
                self.gmail.users()
                .messages()
                .list(userId="me", labelIds=["INBOX"], q="is:unread")
                .execute()
            )
            return [
                m for m in resp.get("messages", [])
                if m["id"] not in self.processed
            ]
        except Exception as exc:
            log.error("Gmail list error: %s", exc)
            return []

    def parse_message(self, msg_id: str) -> Optional[dict]:
        """Return structured sender info extracted from a Gmail message."""
        try:
            msg = (
                self.gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            headers = {
                h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            raw_from = headers.get("From", "")
            name, address = parseaddr(raw_from)
            if not address or "@" not in address:
                return None

            address = address.lower().strip()
            domain = address.split("@")[1]

            parts = name.strip().split() if name.strip() else []
            first = parts[0] if parts else ""
            last = " ".join(parts[1:]) if len(parts) > 1 else ""

            company = ""
            if domain not in FREE_DOMAINS:
                raw = domain.split(".")[0]
                company = raw.replace("-", " ").replace("_", " ").title()

            return {
                "id": msg_id,
                "email": address,
                "first_name": first,
                "last_name": last,
                "full_name": name.strip(),
                "domain": domain,
                "company": company,
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            }
        except Exception as exc:
            log.error("Error parsing message %s: %s", msg_id, exc)
            return None

    # ── HubSpot ───────────────────────────────────────────────────────────────

    def find_contact(self, email: str):
        """Search HubSpot for a contact by email (exact match)."""
        try:
            req = PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(
                        filters=[
                            Filter(
                                property_name="email",
                                operator="EQ",
                                value=email,
                            )
                        ]
                    )
                ],
                properties=["email", "firstname", "lastname", "company"],
                limit=1,
            )
            result = self.hs.crm.contacts.search_api.do_search(
                public_object_search_request=req
            )
            return result.results[0] if result.total > 0 else None
        except Exception as exc:
            log.error("HubSpot search error for %s: %s", email, exc)
            return None

    def create_contact(self, sender: dict) -> Optional[str]:
        """Create a new HubSpot contact. Returns the new contact ID."""
        props: dict[str, str] = {
            "email": sender["email"],
            "hs_lead_status": "NEW",
            "hs_analytics_source": "OFFLINE",  # closest standard value for "Gmail"
        }
        if sender["first_name"]:
            props["firstname"] = sender["first_name"]
        if sender["last_name"]:
            props["lastname"] = sender["last_name"]
        if sender["company"]:
            props["company"] = sender["company"]

        try:
            result = self.hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return result.id
        except ApiException as exc:
            if exc.status == 409:
                # Contact already exists (race condition) — retrieve and return it
                existing = self.find_contact(sender["email"])
                return existing.id if existing else None
            log.error("Error creating contact %s: %s", sender["email"], exc)
            return None

    def update_contact(self, contact_id: str, sender: dict, existing) -> bool:
        """Patch fields that are empty on the existing HubSpot contact."""
        ep = existing.properties or {}
        updates: dict[str, str] = {}
        if not ep.get("firstname") and sender["first_name"]:
            updates["firstname"] = sender["first_name"]
        if not ep.get("lastname") and sender["last_name"]:
            updates["lastname"] = sender["last_name"]
        if not ep.get("company") and sender["company"]:
            updates["company"] = sender["company"]

        if not updates:
            return True

        try:
            self.hs.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=updates
                ),
            )
            return True
        except Exception as exc:
            log.error("Error updating contact %s: %s", contact_id, exc)
            return False

    def add_note(self, contact_id: str, sender: dict):
        """Attach an activity note tagged 'Inbound Gmail' to a contact."""
        body = (
            f"Email ricevuta via Gmail\n"
            f"Da: {sender['full_name']} <{sender['email']}>\n"
            f"Oggetto: {sender['subject']}\n"
            f"Data: {sender['date']}\n"
            f"Fonte: Inbound Gmail"
        )
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        try:
            self.hs.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteCreate(
                    properties={"hs_note_body": body, "hs_timestamp": ts},
                    associations=[
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 202,
                                }
                            ],
                        }
                    ],
                )
            )
        except Exception as exc:
            log.warning("Could not add note for contact %s: %s", contact_id, exc)

    # ── Core logic ────────────────────────────────────────────────────────────

    def _is_skippable(self, email: str) -> bool:
        """Return True for automated/system addresses that should not be synced."""
        local = email.split("@")[0]
        return any(p in local for p in NO_REPLY_PREFIXES)

    def process_message(self, msg_id: str) -> dict:
        """Process one Gmail message. Returns a result dict."""
        result = {
            "message_id": msg_id,
            "status": "IGNORATO",
            "email": "",
            "hubspot_id": None,
        }

        sender = self.parse_message(msg_id)
        if not sender:
            return result

        result["email"] = sender["email"]

        if self._is_skippable(sender["email"]):
            log.debug("Skipping automated address: %s", sender["email"])
            return result

        existing = self.find_contact(sender["email"])
        if existing:
            self.update_contact(existing.id, sender, existing)
            self.add_note(existing.id, sender)
            result.update(status="AGGIORNATO", hubspot_id=existing.id)
        else:
            new_id = self.create_contact(sender)
            if new_id:
                self.add_note(new_id, sender)
                result.update(status="CREATO", hubspot_id=new_id)
            else:
                result["status"] = "ERRORE"

        return result

    def run_once(self) -> list[dict]:
        """Single sync pass: fetch new messages, process each, save state."""
        log.info("Checking Gmail inbox…")
        messages = self.fetch_new_inbox_messages()
        if not messages:
            log.info("No new messages found.")
            return []

        log.info("Processing %d message(s)…", len(messages))
        results: list[dict] = []
        for msg in messages:
            r = self.process_message(msg["id"])
            self.processed.add(msg["id"])
            log.info(
                "  %-12s  %-45s  HubSpot: %s",
                r["status"],
                r["email"],
                r["hubspot_id"] or "—",
            )
            results.append(r)

        self._save_state()
        return results

    def run_continuous(self, interval: int = POLL_INTERVAL):
        """Poll Gmail indefinitely, syncing contacts on each cycle."""
        log.info(
            "Gmail → HubSpot sync started (interval: %ds). Ctrl+C to stop.", interval
        )
        while True:
            try:
                results = self.run_once()
                if results:
                    c = sum(1 for r in results if r["status"] == "CREATO")
                    u = sum(1 for r in results if r["status"] == "AGGIORNATO")
                    i = sum(1 for r in results if r["status"] == "IGNORATO")
                    log.info(
                        "Cycle done — creati: %d  aggiornati: %d  ignorati: %d",
                        c, u, i,
                    )
            except KeyboardInterrupt:
                log.info("Sync stopped by user.")
                break
            except Exception as exc:
                log.error("Unexpected error in sync cycle: %s", exc)

            time.sleep(interval)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Gmail → HubSpot Contact Sync",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--credentials",
        default="credentials.json",
        help="Path to the Gmail OAuth2 client-secrets JSON file",
    )
    parser.add_argument(
        "--hubspot-token",
        default=os.getenv("HUBSPOT_ACCESS_TOKEN"),
        help="HubSpot private-app access token (or set HUBSPOT_ACCESS_TOKEN env var)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL,
        help="Seconds between inbox checks (continuous mode only)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync pass and exit",
    )
    args = parser.parse_args()

    if not args.hubspot_token:
        raise SystemExit(
            "ERROR: provide a HubSpot token via --hubspot-token or HUBSPOT_ACCESS_TOKEN"
        )

    sync = GmailHubSpotSync(
        credentials_file=args.credentials,
        hubspot_token=args.hubspot_token,
    )

    if args.once:
        results = sync.run_once()
        print(f"\n{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
        print("─" * 72)
        for r in results:
            print(
                f"{r['status']:<12} {r['email']:<45} {r['hubspot_id'] or '—'}"
            )
    else:
        sync.run_continuous(interval=args.interval)


if __name__ == "__main__":
    main()
