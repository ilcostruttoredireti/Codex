"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.

Setup:
  1. Copy .env.example to .env and fill in your tokens
  2. Place your Google OAuth credentials.json in this directory
  3. pip install -r requirements.txt
  4. python gmail_hubspot_sync.py
"""

import os
import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
FIRST_RUN_LIMIT = int(os.getenv("FIRST_RUN_LIMIT", "10"))

# Skip automated/noreply senders
SKIP_LOCAL_PATTERNS = re.compile(
    r"(noreply|no[-_]reply|mailer[-_]daemon|postmaster|bounce|notifications?|"
    r"donotreply|do[-_]not[-_]reply|automated?)",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def build_gmail_service():
    creds: Optional[Credentials] = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def gmail_profile(service) -> dict:
    return service.users().getProfile(userId="me").execute()


def fetch_new_message_ids(service, last_history_id: Optional[str]) -> tuple[list[str], str]:
    """
    Returns (new_message_ids, current_history_id).
    On first run (last_history_id is None) returns the FIRST_RUN_LIMIT most recent inbox messages.
    On subsequent runs uses the History API to get only truly new messages.
    """
    profile = gmail_profile(service)
    current_id = profile["historyId"]

    if last_history_id is None:
        result = (
            service.users()
            .messages()
            .list(userId="me", maxResults=FIRST_RUN_LIMIT, labelIds=["INBOX"])
            .execute()
        )
        ids = [m["id"] for m in result.get("messages", [])]
        return ids, current_id

    try:
        history = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )
        ids = [
            msg["message"]["id"]
            for record in history.get("history", [])
            for msg in record.get("messagesAdded", [])
        ]
        return ids, current_id
    except HttpError as e:
        if e.resp.status == 404:
            # historyId expired — fall back to recent messages
            log.warning("historyId expired, falling back to recent messages.")
            result = (
                service.users()
                .messages()
                .list(userId="me", maxResults=FIRST_RUN_LIMIT, labelIds=["INBOX"])
                .execute()
            )
            ids = [m["id"] for m in result.get("messages", [])]
            return ids, current_id
        raise


def fetch_message_headers(service, message_id: str) -> Optional[dict]:
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        return {
            "id": message_id,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
        }
    except HttpError as e:
        log.error("Error fetching message %s: %s", message_id, e)
        return None


# ── Parsing helpers ────────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> tuple[Optional[str], Optional[str]]:
    """Return (full_name, email) from a From header."""
    m = re.match(r'^"?([^"<>]+?)"?\s*<([^>]+)>\s*$', from_header.strip())
    if m:
        return m.group(1).strip() or None, m.group(2).strip().lower()
    email = from_header.strip().lower()
    return None, email if "@" in email else None


def split_name(full_name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not full_name:
        return None, None
    parts = full_name.strip().split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else None


def company_from_domain(email: str) -> Optional[str]:
    """Best-effort company name from domain, e.g. 'acme.com' → 'Acme'."""
    domain = email.split("@")[-1] if "@" in email else ""
    if not domain:
        return None
    name = domain.split(".")[0]
    # Skip generic free/consumer domains
    generic = {
        "gmail", "yahoo", "hotmail", "outlook", "live",
        "icloud", "me", "aol", "protonmail", "zoho",
    }
    return name.capitalize() if name not in generic else None


# ── HubSpot helpers ────────────────────────────────────────────────────────────

def build_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def hs_find_contact(client, email: str) -> Optional[object]:
    """Return existing HubSpot contact object or None."""
    try:
        req = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        res = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        return res.results[0] if res.results else None
    except ApiException as e:
        log.error("HubSpot search error (%s): %s", email, e)
        return None


def hs_create_contact(client, props: dict) -> Optional[str]:
    """Create a new contact. Returns ID or None on failure."""
    try:
        obj = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return obj.id
    except ApiException as e:
        log.error("HubSpot create error: %s", e)
        return None


def hs_update_contact(client, contact_id: str, props: dict) -> bool:
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return True
    except ApiException as e:
        log.error("HubSpot update error (%s): %s", contact_id, e)
        return False


def hs_add_email_note(client, contact_id: str, subject: str, date: str) -> None:
    """Add an activity note to the contact recording the inbound email."""
    try:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        note_body = (
            f"📥 Email in arrivo ricevuta via Gmail\n"
            f"Oggetto: {subject}\n"
            f"Data: {date}\n"
            f"Tag: Inbound Gmail"
        )
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create={
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": str(now_ms),
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
            }
        )
    except Exception as e:
        log.warning("Could not add note for contact %s: %s", contact_id, e)


# ── Core sync logic ────────────────────────────────────────────────────────────

def process_message(gmail_svc, hs_client, message_id: str) -> dict:
    """
    Process a single message and sync sender to HubSpot.
    Returns a result dict with keys: status, email, contact_id.
    """
    result = {"status": "Ignorato", "email": None, "contact_id": None}

    headers = fetch_message_headers(gmail_svc, message_id)
    if not headers or not headers["from"]:
        return result

    full_name, email = parse_sender(headers["from"])

    if not email:
        return result

    result["email"] = email

    # Skip automated senders
    local_part = email.split("@")[0]
    if SKIP_LOCAL_PATTERNS.search(local_part):
        return result

    firstname, lastname = split_name(full_name)
    company = company_from_domain(email)

    new_props: dict[str, str] = {"email": email}
    if firstname:
        new_props["firstname"] = firstname
    if lastname:
        new_props["lastname"] = lastname
    if company:
        new_props["company"] = company
    new_props["hs_lead_status"] = "NEW"

    existing = hs_find_contact(hs_client, email)

    if existing:
        contact_id = existing.id
        result["contact_id"] = contact_id

        # Update only blank fields on the existing contact
        current = existing.properties or {}
        patch = {
            k: v
            for k, v in new_props.items()
            if k != "email" and not current.get(k) and v
        }
        if patch:
            hs_update_contact(hs_client, contact_id, patch)
            result["status"] = "Aggiornato"
        else:
            result["status"] = "Ignorato"
    else:
        contact_id = hs_create_contact(hs_client, new_props)
        result["contact_id"] = contact_id
        result["status"] = "Creato" if contact_id else "Ignorato"

    if result["contact_id"]:
        hs_add_email_note(hs_client, result["contact_id"], headers["subject"], headers["date"])

    return result


# ── Main loop ──────────────────────────────────────────────────────────────────

def run():
    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN non impostato nel file .env")
        raise SystemExit(1)

    log.info("=== Gmail → HubSpot Sync avviato ===")
    log.info("Intervallo di controllo: %ds", CHECK_INTERVAL)

    gmail_svc = build_gmail_service()
    hs_client = build_hubspot_client()

    last_history_id: Optional[str] = None
    seen_ids: set[str] = set()

    while True:
        log.info("── Controllo nuove email ──────────────────────────────")

        try:
            ids, last_history_id = fetch_new_message_ids(gmail_svc, last_history_id)
        except Exception as e:
            log.error("Errore nel recupero email da Gmail: %s", e)
            time.sleep(CHECK_INTERVAL)
            continue

        new_ids = [mid for mid in ids if mid not in seen_ids]
        seen_ids.update(new_ids)

        if not new_ids:
            log.info("  Nessuna nuova email da processare.")
        else:
            log.info("  %d nuove email trovate.", len(new_ids))
            print(
                f"\n{'Stato':<14} {'Email contatto':<45} {'ID HubSpot'}"
            )
            print("─" * 75)
            for mid in new_ids:
                r = process_message(gmail_svc, hs_client, mid)
                print(
                    f"{r['status']:<14} {(r['email'] or 'N/A'):<45} {r['contact_id'] or 'N/A'}"
                )
            print()

        log.info("  Prossimo controllo tra %ds...", CHECK_INTERVAL)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
