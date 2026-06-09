"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.
Avoids duplicates using email as the unique key.
"""

import os
import re
import time
import logging
from dataclasses import dataclass
from email.utils import parseaddr
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicUpsertObjectInput

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
PROCESSED_LABEL_NAME = os.getenv("PROCESSED_LABEL_NAME", "HubSpot-Synced")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox is:unread")
STATE_FILE = os.getenv("STATE_FILE", ".last_history_id")


@dataclass
class SenderInfo:
    email: str
    first_name: str
    last_name: str
    company: str
    raw_name: str


@dataclass
class SyncResult:
    status: str          # Creato | Aggiornato | Ignorato
    email: str
    hubspot_id: Optional[str]
    reason: str = ""


# ─── Gmail helpers ────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")

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


def get_or_create_label(service, name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    result = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    log.info("Label Gmail creata: %s (%s)", name, result["id"])
    return result["id"]


def fetch_new_messages(service, label_id: str) -> list[dict]:
    """Return messages that are NOT yet tagged with our processed label."""
    result = service.users().messages().list(
        userId="me",
        q=f"{GMAIL_QUERY} -label:{PROCESSED_LABEL_NAME}",
        maxResults=50,
    ).execute()
    return result.get("messages", [])


def get_message_headers(service, msg_id: str) -> dict:
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers


def mark_as_processed(service, msg_id: str, label_id: str):
    service.users().messages().modify(
        userId="me", id=msg_id,
        body={"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]},
    ).execute()


# ─── Sender parsing ────────────────────────────────────────────────────────────

_IGNORED_DOMAINS = {"gmail.com", "googlemail.com", "noreply.github.com"}
_NOREPLY_PREFIXES = ("noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster")


def parse_sender(from_header: str) -> Optional[SenderInfo]:
    raw_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None

    local, domain = email_addr.split("@", 1)

    if local.lower().startswith(_NOREPLY_PREFIXES):
        return None

    # Split display name into first/last
    parts = raw_name.strip().split() if raw_name.strip() else []
    first_name = parts[0] if parts else local.split(".")[0].capitalize()
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Derive company from domain (skip generic providers)
    company = ""
    if domain not in _IGNORED_DOMAINS:
        base = domain.split(".")[0]
        company = base.capitalize()

    return SenderInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        raw_name=raw_name,
    )


# ─── HubSpot helpers ──────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN non impostato nel file .env")
    return hubspot.Client.create(access_token=token)


def find_contact_by_email(hs: hubspot.Client, email: str) -> Optional[dict]:
    try:
        results = hs.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
                "limit": 1,
            }
        )
        if results.results:
            return results.results[0]
    except ApiException as e:
        log.error("Errore ricerca contatto HubSpot: %s", e)
    return None


def _build_properties(sender: SenderInfo) -> dict:
    props = {
        "email": sender.email,
        "firstname": sender.first_name,
        "hs_lead_status": "NEW",
        "leadsource": "Gmail",           # standard HubSpot field
        "hs_tag": "Inbound Gmail",        # note: stored as note property
    }
    if sender.last_name:
        props["lastname"] = sender.last_name
    if sender.company:
        props["company"] = sender.company
    return props


def create_contact(hs: hubspot.Client, sender: SenderInfo) -> SyncResult:
    props = _build_properties(sender)
    try:
        contact = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        log.info("✅ CREATO   %s  (ID: %s)", sender.email, contact.id)
        return SyncResult(status="Creato", email=sender.email, hubspot_id=contact.id)
    except ApiException as e:
        if "CONTACT_EXISTS" in str(e):
            return update_or_skip(hs, sender)
        log.error("Errore creazione contatto: %s", e)
        return SyncResult(status="Ignorato", email=sender.email, hubspot_id=None, reason=str(e))


def update_or_skip(hs: hubspot.Client, sender: SenderInfo) -> SyncResult:
    existing = find_contact_by_email(hs, sender.email)
    if not existing:
        return SyncResult(status="Ignorato", email=sender.email, hubspot_id=None, reason="non trovato dopo conflitto")

    contact_id = existing.id
    current = existing.properties or {}

    # Build only the fields that are currently empty/missing
    updates = {}
    if not current.get("firstname") and sender.first_name:
        updates["firstname"] = sender.first_name
    if not current.get("lastname") and sender.last_name:
        updates["lastname"] = sender.last_name
    if not current.get("company") and sender.company:
        updates["company"] = sender.company
    if not current.get("leadsource"):
        updates["leadsource"] = "Gmail"

    if not updates:
        log.info("⏭  IGNORATO  %s  (ID: %s) – nessun campo nuovo", sender.email, contact_id)
        return SyncResult(status="Ignorato", email=sender.email, hubspot_id=contact_id, reason="nessun aggiornamento necessario")

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input={"properties": updates},
        )
        log.info("🔄 AGGIORNATO %s  (ID: %s)  campi: %s", sender.email, contact_id, list(updates.keys()))
        return SyncResult(status="Aggiornato", email=sender.email, hubspot_id=contact_id)
    except ApiException as e:
        log.error("Errore aggiornamento contatto: %s", e)
        return SyncResult(status="Ignorato", email=sender.email, hubspot_id=contact_id, reason=str(e))


def sync_sender(hs: hubspot.Client, sender: SenderInfo) -> SyncResult:
    existing = find_contact_by_email(hs, sender.email)
    if existing:
        return update_or_skip(hs, sender)
    return create_contact(hs, sender)


# ─── Main loop ────────────────────────────────────────────────────────────────

def run_once(gmail, hs: hubspot.Client, label_id: str) -> list[SyncResult]:
    messages = fetch_new_messages(gmail, label_id)
    if not messages:
        log.debug("Nessun nuovo messaggio.")
        return []

    log.info("Trovati %d nuovi messaggi da processare.", len(messages))
    results = []
    seen_emails: set[str] = set()

    for msg in messages:
        msg_id = msg["id"]
        try:
            headers = get_message_headers(gmail, msg_id)
            from_header = headers.get("From", "")
            subject = headers.get("Subject", "(nessun oggetto)")

            sender = parse_sender(from_header)
            if sender is None:
                log.debug("Messaggio ignorato (noreply/invalido): %s", from_header)
                mark_as_processed(gmail, msg_id, label_id)
                continue

            if sender.email in seen_emails:
                mark_as_processed(gmail, msg_id, label_id)
                continue
            seen_emails.add(sender.email)

            log.info("Elaboro: %s | Da: %s", subject[:60], sender.email)
            result = sync_sender(hs, sender)
            results.append(result)

        except Exception as e:
            log.error("Errore sul messaggio %s: %s", msg_id, e)
        finally:
            mark_as_processed(gmail, msg_id, label_id)

    return results


def print_report(results: list[SyncResult]):
    if not results:
        return
    print("\n" + "─" * 62)
    print(f"{'STATO':<12} {'EMAIL':<35} {'ID HUBSPOT'}")
    print("─" * 62)
    for r in results:
        print(f"{r.status:<12} {r.email:<35} {r.hubspot_id or '—'}")
    print("─" * 62)
    creati   = sum(1 for r in results if r.status == "Creato")
    aggiornati = sum(1 for r in results if r.status == "Aggiornato")
    ignorati = sum(1 for r in results if r.status == "Ignorato")
    print(f"Totale: {len(results)}  |  Creati: {creati}  |  Aggiornati: {aggiornati}  |  Ignorati: {ignorati}\n")


def main():
    log.info("Avvio Gmail → HubSpot sync (intervallo: %ds)", POLL_INTERVAL_SECONDS)

    gmail = get_gmail_service()
    hs = get_hubspot_client()
    label_id = get_or_create_label(gmail, PROCESSED_LABEL_NAME)

    log.info("Label di tracking: '%s' (ID: %s)", PROCESSED_LABEL_NAME, label_id)

    while True:
        try:
            results = run_once(gmail, hs, label_id)
            print_report(results)
        except KeyboardInterrupt:
            log.info("Sync interrotto dall'utente.")
            break
        except Exception as e:
            log.error("Errore nel ciclo principale: %s", e)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
