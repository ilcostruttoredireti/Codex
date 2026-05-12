#!/usr/bin/env python3
"""Gmail → HubSpot Contact Sync

Monitors incoming Gmail messages and automatically syncs sender contacts
to HubSpot, avoiding duplicates and updating existing records.

Usage:
    python gmail_hubspot_sync.py [--interval 60] [--backfill] [--no-timeline]

Environment variables required:
    HUBSPOT_API_KEY  - HubSpot Private App access token

Google credentials:
    Place credentials.json (OAuth 2.0 client) in the working directory.
    Token is cached to token.json after first interactive login.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

# Free email providers whose domains don't represent company names
_FREE_PROVIDERS = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.it",
        "hotmail.com",
        "hotmail.it",
        "outlook.com",
        "outlook.it",
        "live.com",
        "live.it",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "libero.it",
        "alice.it",
        "tiscali.it",
        "virgilio.it",
        "email.it",
        "tin.it",
        "iol.it",
    ]
)

# Addresses that should never be synced
_SKIP_PATTERNS = [
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "notifications@",
    "support@",
    "info@",
    "newsletter",
    "unsubscribe",
    "reply-to-",
    "automated",
    "system@",
]


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------


class GmailClient:
    def __init__(self, credentials_file: str, token_file: str) -> None:
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()

    def _authenticate(self):
        creds: Optional[Credentials] = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(self.credentials_file):
                    raise FileNotFoundError(
                        f"Google credentials file not found: {self.credentials_file}\n"
                        "Download it from the Google Cloud Console (OAuth 2.0 Client ID)."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            with open(self.token_file, "w") as fh:
                fh.write(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def get_history_id(self) -> str:
        profile = self.service.users().getProfile(userId="me").execute()
        return profile["historyId"]

    def list_new_message_ids(self, start_history_id: str) -> list[str]:
        """Return IDs of INBOX messages added since start_history_id."""
        ids: list[str] = []
        try:
            resp = (
                self.service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    labels = msg.get("labelIds", [])
                    if "INBOX" in labels and "SENT" not in labels:
                        ids.append(msg["id"])
        except HttpError as exc:
            if exc.resp.status == 404:
                logger.warning("History ID expired or invalid; resetting cursor.")
            else:
                raise
        return ids

    def list_inbox_message_ids(self, max_results: int = 100) -> list[str]:
        """Return recent INBOX message IDs (used for initial backfill)."""
        ids: list[str] = []
        page_token = None
        while True:
            kwargs: dict = {"userId": "me", "labelIds": ["INBOX"], "maxResults": min(max_results - len(ids), 100)}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self.service.users().messages().list(**kwargs).execute()
            for msg in resp.get("messages", []):
                ids.append(msg["id"])
            if len(ids) >= max_results:
                break
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_sender_info(self, message_id: str) -> Optional[dict]:
        """Extract sender details from a Gmail message header."""
        try:
            msg = (
                self.service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
        except HttpError as exc:
            logger.warning(f"Could not fetch message {message_id}: {exc}")
            return None

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")

        name, email = parseaddr(from_header)
        email = email.lower().strip()

        if not email or "@" not in email:
            return None

        domain = email.split("@")[1]
        return {
            "email": email,
            "name": name.strip(),
            "domain": domain,
            "subject": subject,
            "message_id": message_id,
        }


# ---------------------------------------------------------------------------
# HubSpot client
# ---------------------------------------------------------------------------


class HubSpotClient:
    _BASE = "https://api.hubapi.com"

    def __init__(self, access_token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _req(self, method: str, path: str, **kwargs) -> dict:
        resp = requests.request(
            method, f"{self._BASE}{path}", headers=self._headers, timeout=15, **kwargs
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return existing contact dict or None."""
        try:
            data = self._req(
                "POST",
                "/crm/v3/objects/contacts/search",
                json={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "email",
                                    "operator": "EQ",
                                    "value": email,
                                }
                            ]
                        }
                    ],
                    "properties": [
                        "email",
                        "firstname",
                        "lastname",
                        "company",
                        "hs_lead_status",
                    ],
                    "limit": 1,
                },
            )
            results = data.get("results", [])
            return results[0] if results else None
        except requests.HTTPError as exc:
            logger.error(f"HubSpot search failed: {exc}")
            return None

    def create_contact(self, properties: dict) -> Optional[str]:
        """Create a contact and return its ID."""
        try:
            data = self._req(
                "POST",
                "/crm/v3/objects/contacts",
                json={"properties": properties},
            )
            return data.get("id")
        except requests.HTTPError as exc:
            logger.error(f"HubSpot create contact failed: {exc}")
            return None

    def update_contact(self, contact_id: str, properties: dict) -> bool:
        """Patch a contact with the given properties."""
        try:
            self._req(
                "PATCH",
                f"/crm/v3/objects/contacts/{contact_id}",
                json={"properties": properties},
            )
            return True
        except requests.HTTPError as exc:
            logger.error(f"HubSpot update contact failed: {exc}")
            return False

    def add_note(self, contact_id: str, body: str) -> bool:
        """Create a note associated with a contact."""
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        try:
            self._req(
                "POST",
                "/crm/v3/objects/notes",
                json={
                    "properties": {
                        "hs_note_body": body,
                        "hs_timestamp": str(ts),
                    },
                    "associations": [
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
                },
            )
            return True
        except requests.HTTPError as exc:
            logger.error(f"HubSpot add note failed: {exc}")
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """Infer company name from domain; return empty string for free providers."""
    if domain.lower() in _FREE_PROVIDERS:
        return ""
    # "acme-corp.io" → "Acme Corp"
    stem = domain.split(".")[0]
    return stem.replace("-", " ").replace("_", " ").title()


def _should_skip(email: str) -> bool:
    low = email.lower()
    return any(pat in low for pat in _SKIP_PATTERNS)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------


def sync_sender(sender: dict, hubspot: HubSpotClient, add_timeline: bool) -> dict:
    """
    Sync a single sender to HubSpot.

    Returns:
        {"status": "Creato"|"Aggiornato"|"Ignorato"|"Errore",
         "email": str, "contact_id": str|None}
    """
    email = sender["email"]
    result: dict = {"email": email, "contact_id": None, "status": "Ignorato"}

    if _should_skip(email):
        logger.debug(f"Skipping system/automated address: {email}")
        return result

    firstname, lastname = _split_name(sender.get("name", ""))
    company = _company_from_domain(sender["domain"])
    subject = sender.get("subject", "")

    existing = hubspot.find_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates: dict = {}

        if not props.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not props.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not props.get("company") and company:
            updates["company"] = company

        if updates:
            hubspot.update_contact(contact_id, updates)

        if add_timeline:
            hubspot.add_note(
                contact_id,
                f"📧 Email ricevuta\nMittente: {email}\nOggetto: {subject}\nTag: Inbound Gmail",
            )

        result.update({"status": "Aggiornato", "contact_id": contact_id})
        logger.info(f"Updated  {email} → HubSpot ID {contact_id}")

    else:
        new_props: dict = {
            "email": email,
            "leadsource": "EMAIL_MARKETING",
            "hs_analytics_source_data_1": "Inbound Gmail",
        }
        if firstname:
            new_props["firstname"] = firstname
        if lastname:
            new_props["lastname"] = lastname
        if company:
            new_props["company"] = company

        contact_id = hubspot.create_contact(new_props)
        if contact_id:
            if add_timeline:
                hubspot.add_note(
                    contact_id,
                    f"📧 Prima email ricevuta\nMittente: {email}\nOggetto: {subject}\nTag: Inbound Gmail",
                )
            result.update({"status": "Creato", "contact_id": contact_id})
            logger.info(f"Created  {email} → HubSpot ID {contact_id}")
        else:
            result["status"] = "Errore"
            logger.error(f"Failed to create contact for {email}")

    return result


def _print_result(result: dict) -> None:
    status_icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️", "Errore": "❌"}.get(
        result["status"], "?"
    )
    ts = datetime.now().strftime("%H:%M:%S")
    contact_id = result["contact_id"] or "N/A"
    print(
        f"[{ts}] {status_icon} Stato: {result['status']:<12} | "
        f"Email: {result['email']:<40} | ID HubSpot: {contact_id}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    gmail: GmailClient,
    hubspot: HubSpotClient,
    poll_interval: int,
    add_timeline: bool,
    backfill: bool,
    backfill_limit: int,
) -> None:
    processed: set[str] = set()

    if backfill:
        logger.info(f"Backfill mode: processing up to {backfill_limit} existing INBOX messages...")
        ids = gmail.list_inbox_message_ids(max_results=backfill_limit)
        for msg_id in ids:
            if msg_id in processed:
                continue
            processed.add(msg_id)
            sender = gmail.get_sender_info(msg_id)
            if sender:
                _print_result(sync_sender(sender, hubspot, add_timeline))

    history_id = gmail.get_history_id()
    logger.info(f"Watching for new messages (history ID: {history_id}, poll every {poll_interval}s)...")

    while True:
        time.sleep(poll_interval)
        try:
            new_ids = gmail.list_new_message_ids(history_id)
            history_id = gmail.get_history_id()

            for msg_id in new_ids:
                if msg_id in processed:
                    continue
                processed.add(msg_id)
                sender = gmail.get_sender_info(msg_id)
                if sender:
                    _print_result(sync_sender(sender, hubspot, add_timeline))

        except Exception as exc:
            logger.error(f"Error during poll cycle: {exc}", exc_info=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gmail → HubSpot Contact Sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--credentials",
        default=os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
        help="Path to Google OAuth 2.0 credentials JSON (default: credentials.json)",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GOOGLE_TOKEN_FILE", "token.json"),
        help="Path to cached OAuth token (default: token.json)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL", "60")),
        help="Poll interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Process existing INBOX messages on startup before watching for new ones",
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        default=int(os.getenv("BACKFILL_LIMIT", "100")),
        help="Max messages to backfill (default: 100)",
    )
    parser.add_argument(
        "--no-timeline",
        action="store_true",
        help="Disable HubSpot note/timeline entries",
    )

    args = parser.parse_args()

    api_key = os.getenv("HUBSPOT_API_KEY")
    if not api_key:
        sys.exit(
            "Error: HUBSPOT_API_KEY is not set.\n"
            "Create a .env file with HUBSPOT_API_KEY=<your-private-app-token>."
        )

    gmail = GmailClient(args.credentials, args.token)
    hubspot = HubSpotClient(api_key)

    run(
        gmail=gmail,
        hubspot=hubspot,
        poll_interval=args.interval,
        add_timeline=not args.no_timeline,
        backfill=args.backfill,
        backfill_limit=args.backfill_limit,
    )


if __name__ == "__main__":
    main()
