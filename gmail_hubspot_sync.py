#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail inbox, extracts sender contacts, syncs to HubSpot CRM.

Usage:
    python gmail_hubspot_sync.py            # continuous polling loop
    python gmail_hubspot_sync.py --once     # process current inbox once and exit
    python gmail_hubspot_sync.py --dry-run  # show what would happen, no writes
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

# ── Google ──────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── HubSpot ──────────────────────────────────────────────────────────────────
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException

# ─────────────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

PROCESSED_LABEL_NAME = "Inbound Gmail"
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "icloud.com", "live.com", "live.it",
    "msn.com", "aol.com", "protonmail.com", "proton.me",
    "me.com", "mac.com", "libero.it", "virgilio.it", "tiscali.it",
    "alice.it", "tin.it", "fastwebnet.it",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    full_name: str
    domain: str
    company: str


@dataclass
class SyncResult:
    status: str           # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: Optional[str]
    subject: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "email": self.email,
            "hubspot_id": self.hubspot_id,
            "subject": self.subject,
        }


# ─── Parsing helpers ──────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> SenderInfo:
    """Parse RFC 5322 From header into structured sender info."""
    full_name, email = parseaddr(from_header)
    email = re.sub(r"\s+", "", email).lower()

    domain = email.split("@")[1] if "@" in email else ""
    company = ""
    if domain and domain not in GENERIC_DOMAINS:
        # Use the second-level domain label as the company hint
        parts = domain.split(".")
        company = parts[-2].capitalize() if len(parts) >= 2 else parts[0].capitalize()

    name_parts = full_name.strip().split(" ", 1) if full_name.strip() else []
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    return SenderInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name.strip(),
        domain=domain,
        company=company,
    )


# ─── Gmail client ─────────────────────────────────────────────────────────────

class GmailClient:
    def __init__(
        self,
        credentials_file: str = "credentials.json",
        token_file: str = "token.json",
    ):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self.service = self._authenticate()
        self._label_id: Optional[str] = None

    def _authenticate(self):
        creds: Optional[Credentials] = None
        if Path(self.token_file).exists():
            creds = Credentials.from_authorized_user_file(self.token_file, GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_file, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)
            Path(self.token_file).write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    @property
    def label_id(self) -> str:
        if self._label_id is None:
            self._label_id = self._get_or_create_label(PROCESSED_LABEL_NAME)
        return self._label_id

    def _get_or_create_label(self, name: str) -> str:
        result = self.service.users().labels().list(userId="me").execute()
        for lbl in result.get("labels", []):
            if lbl["name"] == name:
                return lbl["id"]
        created = self.service.users().labels().create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
                "color": {"backgroundColor": "#4a86e8", "textColor": "#ffffff"},
            },
        ).execute()
        log.info("Created Gmail label '%s' (%s)", name, created["id"])
        return created["id"]

    def fetch_unprocessed_inbox(self, max_results: int = 50) -> list[dict]:
        """Return inbox messages not yet labelled with PROCESSED_LABEL_NAME."""
        query = f'in:inbox -label:"{PROCESSED_LABEL_NAME}"'
        response = (
            self.service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return response.get("messages", [])

    def get_message_headers(self, msg_id: str) -> dict[str, str]:
        """Return a name→value dict of selected headers."""
        msg = (
            self.service.users()
            .messages()
            .get(
                userId="me",
                messageId=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date", "To"],
            )
            .execute()
        )
        return {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

    def mark_processed(self, msg_id: str) -> None:
        self.service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [self.label_id]},
        ).execute()


# ─── HubSpot client ───────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, access_token: str):
        self.client = hubspot.Client.create(access_token=access_token)

    def find_by_email(self, email: str) -> Optional[dict]:
        try:
            contact = self.client.crm.contacts.basic_api.get_by_id(
                contact_id=email,
                id_property="email",
                properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
            )
            return contact.to_dict()
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise

    def create_contact(self, sender: SenderInfo) -> dict:
        props: dict[str, str] = {
            "email": sender.email,
            "hs_lead_source": "Gmail",
        }
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company

        result = self.client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.to_dict()

    def update_contact(self, contact_id: str, sender: SenderInfo, existing_props: dict) -> bool:
        """Fill only missing fields. Returns True if anything was updated."""
        updates: dict[str, str] = {}
        if not existing_props.get("firstname") and sender.first_name:
            updates["firstname"] = sender.first_name
        if not existing_props.get("lastname") and sender.last_name:
            updates["lastname"] = sender.last_name
        if not existing_props.get("company") and sender.company:
            updates["company"] = sender.company

        if updates:
            self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            return True
        return False

    def add_email_activity(self, contact_id: str, sender: SenderInfo, subject: str) -> None:
        """Log the received email as a HubSpot EMAIL engagement."""
        try:
            engagement_api = self.client.crm.objects.basic_api
            # Use the simple engagements endpoint via raw API call
            self.client.api_client.call_api(
                "/engagements/v1/engagements",
                "POST",
                body={
                    "engagement": {
                        "active": True,
                        "type": "EMAIL",
                        "timestamp": int(time.time() * 1000),
                    },
                    "associations": {
                        "contactIds": [int(contact_id)],
                    },
                    "metadata": {
                        "from": {"email": sender.email, "firstName": sender.first_name},
                        "subject": subject,
                        "direction": "INBOUND",
                    },
                },
            )
        except Exception as exc:
            log.debug("Could not log email engagement: %s", exc)


# ─── Core sync logic ──────────────────────────────────────────────────────────

def sync_message(
    gmail: GmailClient,
    hs: HubSpotClient,
    msg_id: str,
    my_email: str,
    dry_run: bool = False,
) -> SyncResult:
    headers = gmail.get_message_headers(msg_id)
    from_header = headers.get("From", "").strip()
    subject = headers.get("Subject", "(nessun oggetto)")

    if not from_header:
        return SyncResult("Ignorato", "", None, subject, "From header assente")

    sender = parse_sender(from_header)

    if not sender.email or sender.email == my_email:
        if not dry_run:
            gmail.mark_processed(msg_id)
        return SyncResult("Ignorato", sender.email, None, subject, "Email propria o non valida")

    if dry_run:
        return SyncResult("(dry-run)", sender.email, None, subject, f"Mittente: {sender.full_name}")

    existing = hs.find_by_email(sender.email)

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        hs.update_contact(contact_id, sender, existing_props)
        hs.add_email_activity(contact_id, sender, subject)
        gmail.mark_processed(msg_id)
        return SyncResult("Aggiornato", sender.email, contact_id, subject)
    else:
        created = hs.create_contact(sender)
        contact_id = created["id"]
        hs.add_email_activity(contact_id, sender, subject)
        gmail.mark_processed(msg_id)
        return SyncResult("Creato", sender.email, contact_id, subject)


def run_once(gmail: GmailClient, hs: HubSpotClient, my_email: str, dry_run: bool) -> list[SyncResult]:
    messages = gmail.fetch_unprocessed_inbox()
    if not messages:
        log.info("Nessun nuovo messaggio da processare.")
        return []

    log.info("Trovati %d messaggi da processare.", len(messages))
    results: list[SyncResult] = []
    for msg in messages:
        try:
            result = sync_message(gmail, hs, msg["id"], my_email, dry_run)
            results.append(result)
            log.info("[%s] %-40s  HubSpot ID: %s", result.status, result.email, result.hubspot_id)
        except Exception as exc:
            log.error("Errore sul messaggio %s: %s", msg["id"], exc, exc_info=True)

    return results


def run_loop(gmail: GmailClient, hs: HubSpotClient, my_email: str, interval: int) -> None:
    log.info("Avvio Gmail → HubSpot sync (polling ogni %ds)...", interval)
    while True:
        try:
            run_once(gmail, hs, my_email, dry_run=False)
        except HttpError as exc:
            log.error("Gmail API error: %s", exc)
        except Exception as exc:
            log.error("Errore imprevisto: %s", exc, exc_info=True)
        log.debug("Attendo %ds prima del prossimo ciclo...", interval)
        time.sleep(interval)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    p.add_argument("--once", action="store_true", help="Processa inbox corrente ed esci")
    p.add_argument("--dry-run", action="store_true", help="Simula senza scrivere")
    p.add_argument("--json", action="store_true", dest="json_out", help="Output JSON su stdout")
    p.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL", "60")),
        help="Secondi tra i cicli di polling (default: 60)",
    )
    p.add_argument(
        "--credentials",
        default=os.getenv("GMAIL_CREDENTIALS", "credentials.json"),
        help="Percorso credentials.json OAuth Google",
    )
    p.add_argument(
        "--token",
        default=os.getenv("GMAIL_TOKEN", "token.json"),
        help="Percorso token.json OAuth Google",
    )
    return p


def main() -> None:
    args = build_arg_parser().parse_args()

    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        sys.exit("Errore: variabile d'ambiente HUBSPOT_ACCESS_TOKEN non impostata.")

    my_email = os.getenv("MY_EMAIL", "").lower().strip()

    gmail = GmailClient(credentials_file=args.credentials, token_file=args.token)
    hs = HubSpotClient(access_token=hubspot_token)

    if args.once or args.dry_run:
        results = run_once(gmail, hs, my_email, dry_run=args.dry_run)
        if args.json_out:
            print(json.dumps([r.as_dict() for r in results], ensure_ascii=False, indent=2))
        for r in results:
            print(f"[{r.status}]  {r.email}  →  ID: {r.hubspot_id}")
    else:
        run_loop(gmail, hs, my_email, interval=args.interval)


if __name__ == "__main__":
    main()
