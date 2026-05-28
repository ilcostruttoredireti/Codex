#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and automatically syncs sender contacts to HubSpot.

Setup:
  1. Set HUBSPOT_ACCESS_TOKEN in .env or environment
  2. Place Google OAuth credentials.json in the project root
  3. Run once: python gmail_hubspot_sync.py (browser will open for Gmail auth)
  4. Subsequent runs use the saved token.json
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from hubspot import HubSpot
from hubspot.crm.contacts import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("gmail_hubspot_sync.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDS_FILE = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path("token.json")
STATE_FILE = Path("sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MAX_EMAILS_PER_CYCLE = int(os.getenv("MAX_EMAILS_PER_CYCLE", "50"))

# Personal / generic domains — senders from these are not synced to HubSpot
# (they are still processed as individuals but company is left blank)
CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "yahoo.fr", "yahoo.es", "hotmail.com", "hotmail.it", "hotmail.fr",
    "outlook.com", "live.com", "live.it", "msn.com", "icloud.com", "me.com",
    "mac.com", "aol.com", "protonmail.com", "proton.me", "libero.it",
    "alice.it", "virgilio.it", "tin.it", "tiscali.it", "fastwebnet.it",
}

# Prefixes of email addresses to ignore entirely
SKIP_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces", "notifications",
    "newsletter", "newsletters", "marketing", "updates", "alerts",
    "autoresponse", "auto-response", "robot", "daemon",
)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("State file corrupted — starting fresh")
    return {"last_history_id": None, "processed_ids": []}


def save_state(state: dict) -> None:
    # Keep only the most recent 2000 processed IDs
    state["processed_ids"] = state.get("processed_ids", [])[-2000:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Gmail authentication & helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                log.error(
                    f"Google credentials file not found: {CREDS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials"
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, state: dict) -> list[dict]:
    """
    Return list of {id} dicts for new inbox messages since the last run.
    Uses Gmail History API when a history_id is available, otherwise falls
    back to a full inbox listing (first run or after history expiry).
    """
    try:
        if state.get("last_history_id"):
            try:
                history_resp = service.users().history().list(
                    userId="me",
                    startHistoryId=state["last_history_id"],
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                ).execute()

                messages = []
                for record in history_resp.get("history", []):
                    for added in record.get("messagesAdded", []):
                        label_ids = added["message"].get("labelIds", [])
                        if "INBOX" in label_ids and "DRAFT" not in label_ids:
                            messages.append({"id": added["message"]["id"]})

                if "historyId" in history_resp:
                    state["last_history_id"] = history_resp["historyId"]

                return messages

            except HttpError as exc:
                if exc.resp.status == 404:
                    log.warning("Gmail history expired — falling back to full inbox scan")
                    state["last_history_id"] = None
                else:
                    raise

        # First run or history expired: list recent inbox messages
        result = service.users().messages().list(
            userId="me",
            q="in:inbox -from:me -is:draft",
            maxResults=MAX_EMAILS_PER_CYCLE,
        ).execute()

        # Record current historyId so next run uses the delta API
        profile = service.users().getProfile(userId="me").execute()
        state["last_history_id"] = profile.get("historyId")

        return result.get("messages", [])

    except HttpError as exc:
        log.error(f"Gmail API error: {exc}")
        return []


def get_message_headers(service, message_id: str) -> dict | None:
    """Fetch only the From/Subject/Date headers to minimise quota usage."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        return {
            "id": message_id,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        }
    except HttpError as exc:
        log.warning(f"Could not fetch message {message_id}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Sender parsing
# ---------------------------------------------------------------------------

def parse_sender(from_header: str) -> dict:
    """Parse 'Display Name <email@domain.com>' into structured fields."""
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    first_name, last_name = "", ""
    if display_name:
        parts = display_name.strip().split(None, 1)  # split on first whitespace
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    domain = email.split("@")[-1] if "@" in email else ""
    company = _company_from_domain(domain)

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
    }


def _company_from_domain(domain: str) -> str:
    if not domain or domain in CONSUMER_DOMAINS:
        return ""
    # Strip leading "mail.", "email.", "m." etc.
    parts = domain.split(".")
    skip_subdomains = {"mail", "email", "m", "smtp", "mx", "info"}
    while len(parts) > 2 and parts[0] in skip_subdomains:
        parts = parts[1:]
    name = parts[0] if parts else ""
    return name.replace("-", " ").replace("_", " ").title()


def should_skip(email: str) -> bool:
    local = email.split("@")[0].lower() if "@" in email else email.lower()
    return any(local.startswith(p) for p in SKIP_PREFIXES)


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def search_contact(hs: HubSpot, email: str):
    """Return existing HubSpot contact or None."""
    try:
        result = hs.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(
                        filters=[Filter(property_name="email", operator="EQ", value=email)]
                    )
                ],
                properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
                limit=1,
            )
        )
        return result.results[0] if result.total > 0 else None
    except ApiException as exc:
        log.error(f"HubSpot search failed for {email}: {exc}")
        return None


def create_contact(hs: HubSpot, sender: dict) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (status_label, contact_id)."""
    props = {
        "email": sender["email"],
        "leadsource": "OTHER_CAMPAIGNS",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    try:
        contact = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return "CREATO", contact.id
    except ApiException as exc:
        log.error(f"HubSpot create failed for {sender['email']}: {exc}")
        return "ERRORE", ""


def update_contact(hs: HubSpot, contact_id: str, sender: dict, existing) -> tuple[str, str]:
    """Fill in any blank fields on an existing contact."""
    existing_props = existing.properties or {}
    updates = {}

    if sender["first_name"] and not existing_props.get("firstname"):
        updates["firstname"] = sender["first_name"]
    if sender["last_name"] and not existing_props.get("lastname"):
        updates["lastname"] = sender["last_name"]
    if sender["company"] and not existing_props.get("company"):
        updates["company"] = sender["company"]

    if not updates:
        return "IGNORATO", contact_id

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return "AGGIORNATO", contact_id
    except ApiException as exc:
        log.error(f"HubSpot update failed for {contact_id}: {exc}")
        return "ERRORE", contact_id


def create_activity_note(hs: HubSpot, contact_id: str, email_data: dict) -> None:
    """Attach an email-received note to the HubSpot contact."""
    try:
        timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        note_body = (
            f"📧 Email ricevuta via Gmail\n"
            f"Oggetto: {email_data.get('subject', '(nessun oggetto)')}\n"
            f"Data: {email_data.get('date', '')}\n"
            f"Tag: Inbound Gmail | Fonte: Gmail"
        )
        note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": note_body,
                    "hs_timestamp": timestamp_ms,
                }
            )
        )
        hs.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:
        log.warning(f"Could not create activity note for contact {contact_id}: {exc}")


# ---------------------------------------------------------------------------
# Core processing loop
# ---------------------------------------------------------------------------

def process_message(gmail, hs: HubSpot, message_id: str, state: dict) -> dict | None:
    """
    Process one Gmail message.
    Returns a result dict or None if the message was skipped.
    """
    if message_id in state.get("processed_ids", []):
        return None

    # Always mark as processed to avoid retry storms on errors
    state.setdefault("processed_ids", []).append(message_id)

    email_data = get_message_headers(gmail, message_id)
    if not email_data or not email_data["from"]:
        return None

    sender = parse_sender(email_data["from"])
    if not sender["email"] or "@" not in sender["email"]:
        return None
    if should_skip(sender["email"]):
        log.debug(f"  Skipped (automated sender): {sender['email']}")
        return None

    log.info(f"  → Processing: {sender['email']}")

    existing = search_contact(hs, sender["email"])
    if existing:
        status, contact_id = update_contact(hs, existing.id, sender, existing)
    else:
        status, contact_id = create_contact(hs, sender)

    if contact_id and status != "ERRORE":
        create_activity_note(hs, contact_id, email_data)

    return {"stato": status, "email": sender["email"], "contatto_id": contact_id}


def run_sync_cycle(gmail, hs: HubSpot, state: dict) -> None:
    log.info("─── Controllo nuove email ───")
    messages = fetch_new_messages(gmail, state)

    if not messages:
        log.info("Nessuna nuova email.")
        return

    log.info(f"Trovate {len(messages)} email da elaborare")

    stats = {"CREATO": 0, "AGGIORNATO": 0, "IGNORATO": 0, "ERRORE": 0}
    for msg in messages:
        result = process_message(gmail, hs, msg["id"], state)
        if result:
            stats[result["stato"]] = stats.get(result["stato"], 0) + 1
            icon = {"CREATO": "✅", "AGGIORNATO": "🔄", "IGNORATO": "➖", "ERRORE": "❌"}.get(
                result["stato"], "•"
            )
            log.info(
                f"  {icon} {result['stato']:10s} | "
                f"Email: {result['email']:40s} | "
                f"ID HubSpot: {result['contatto_id']}"
            )

    log.info(
        f"Ciclo completato — "
        f"Creati: {stats['CREATO']} | "
        f"Aggiornati: {stats['AGGIORNATO']} | "
        f"Ignorati: {stats['IGNORATO']} | "
        f"Errori: {stats['ERRORE']}"
    )


def main() -> None:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        log.error(
            "HUBSPOT_ACCESS_TOKEN non trovato.\n"
            "Imposta la variabile d'ambiente o aggiungila al file .env"
        )
        sys.exit(1)

    log.info("=" * 60)
    log.info("  Gmail → HubSpot Contact Sync")
    log.info("=" * 60)
    log.info(f"  Poll interval: {POLL_INTERVAL}s")
    log.info(f"  Max emails/cycle: {MAX_EMAILS_PER_CYCLE}")

    gmail = get_gmail_service()
    hs = HubSpot(access_token=token)
    state = load_state()

    log.info("Connesso a Gmail e HubSpot. Monitoraggio avviato.\n")

    try:
        while True:
            run_sync_cycle(gmail, hs, state)
            save_state(state)
            log.info(f"Prossimo controllo tra {POLL_INTERVAL}s...\n")
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("\nInterruzione ricevuta. Salvataggio stato e uscita.")
        save_state(state)
        sys.exit(0)
    except Exception as exc:
        log.error(f"Errore critico: {exc}", exc_info=True)
        save_state(state)
        sys.exit(1)


if __name__ == "__main__":
    main()
