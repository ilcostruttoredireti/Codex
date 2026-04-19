"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously and syncs sender contacts to HubSpot.

Setup:
  1. Create a Google Cloud project, enable Gmail API, download credentials.json
  2. Create a HubSpot Private App with contacts read/write scope
  3. Copy .env.example → .env and fill in values
  4. Run: python gmail_hubspot_sync.py
"""

import os
import time
import json
import logging
import re
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts.models import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = ".sync_state.json"

# Free email providers — domain is not a useful company signal
_FREE_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "protonmail.com", "mail.com", "aol.com", "live.com", "msn.com",
    "me.com", "mac.com", "googlemail.com", "ymail.com",
}

# Patterns for automated / no-reply addresses to skip
_SKIP_PATTERNS = re.compile(
    r"(no.?reply|noreply|donotreply|notifications?@|mailer-daemon|postmaster"
    r"|bounce|newsletter|unsubscribe|automail|alert@|daemon@)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_gmail_service():
    token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
    creds = None

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(creds_path).exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at '{creds_path}'. "
                    "Download credentials.json from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _get_hubspot_client():
    api_key = os.getenv("HUBSPOT_API_KEY")
    if not api_key:
        raise ValueError("HUBSPOT_API_KEY is not set. Check your .env file.")
    return hubspot.Client.create(access_token=api_key)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _decode_mime_words(value: str) -> str:
    """Decode RFC 2047 encoded words in a header value."""
    from email.header import decode_header
    parts = []
    for raw, charset in decode_header(value):
        if isinstance(raw, bytes):
            parts.append(raw.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(raw)
    return "".join(parts)


def _parse_from_header(from_header: str) -> Tuple[str, Optional[str], str]:
    """
    Returns (email_address, display_name_or_None, domain).
    Handles 'Name <addr>' and bare 'addr' formats.
    """
    value = _decode_mime_words(from_header).strip()
    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', value)
    if match:
        name = match.group(1).strip() or None
        addr = match.group(2).strip().lower()
    else:
        name = None
        addr = value.lower()

    domain_match = re.search(r"@(.+)$", addr)
    domain = domain_match.group(1).strip() if domain_match else ""
    return addr, name, domain


def _split_name(full_name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if not full_name:
        return None, None
    parts = full_name.strip().split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else None


def _company_from_domain(domain: str) -> Optional[str]:
    if not domain or domain.lower() in _FREE_PROVIDERS:
        return None
    # "acme.co.uk" → "Acme"
    return domain.split(".")[0].capitalize()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_history_id": None, "processed_ids": []}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _fetch_new_message_ids(
    service, last_history_id: Optional[str]
) -> Tuple[list, str]:
    """
    Returns (list_of_new_message_ids, current_history_id).
    On first run (no last_history_id) returns the 50 most recent inbox messages.
    """
    profile = service.users().getProfile(userId="me").execute()
    current_history_id = profile["historyId"]

    if not last_history_id:
        result = service.users().messages().list(
            userId="me", labelIds=["INBOX"], maxResults=50
        ).execute()
        ids = [m["id"] for m in result.get("messages", [])]
        return ids, current_history_id

    try:
        history_resp = service.users().history().list(
            userId="me",
            startHistoryId=last_history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()

        ids = []
        for record in history_resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added["message"]
                if "INBOX" in msg.get("labelIds", []):
                    ids.append(msg["id"])

        return ids, current_history_id

    except HttpError as exc:
        if exc.resp.status == 404:
            # History ID expired; reset and process nothing this cycle
            logger.warning("Gmail history ID expired — resetting state")
            return [], current_history_id
        raise


def _get_from_header(service, message_id: str) -> Optional[str]:
    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="metadata",
        metadataHeaders=["From"],
    ).execute()
    for header in msg.get("payload", {}).get("headers", []):
        if header["name"].lower() == "from":
            return header["value"]
    return None


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _find_contact(client, email: str) -> Optional[object]:
    """Search HubSpot for a contact by email; returns the result object or None."""
    try:
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
            ],
            properties=["email", "firstname", "lastname", "company"],
            limit=1,
        )
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        return resp.results[0] if resp.total > 0 else None
    except ApiException as exc:
        logger.error("HubSpot search failed for %s: %s", email, exc)
        return None


def _create_contact(client, props: dict) -> Optional[str]:
    try:
        resp = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return resp.id
    except ApiException as exc:
        logger.error("HubSpot create failed: %s", exc)
        return None


def _update_contact(client, contact_id: str, props: dict) -> bool:
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return True
    except ApiException as exc:
        logger.error("HubSpot update failed for %s: %s", contact_id, exc)
        return False


def _add_note(client, contact_id: str, body: str):
    """Create a note engagement associated with the contact."""
    try:
        from hubspot.crm.objects.notes.models import SimplePublicObjectInputForCreate as NoteInput
        note_props = {
            "hs_note_body": body,
            "hs_timestamp": str(int(time.time() * 1000)),
        }
        note_resp = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteInput(properties=note_props)
        )
        # Associate note with contact
        client.crm.objects.notes.associations_api.create(
            note_id=note_resp.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:
        # Notes are best-effort; log and continue
        logger.debug("Could not add note to contact %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def _sync_sender(client, email_addr: str, name: Optional[str], domain: str) -> dict:
    """
    Ensure the sender exists in HubSpot.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato"|"Errore", "email": ..., "contact_id": ...}
    """
    first_name, last_name = _split_name(name)
    company = _company_from_domain(domain)

    existing = _find_contact(client, email_addr)

    if existing:
        contact_id = existing.id
        ep = existing.properties or {}
        updates = {}
        if first_name and not ep.get("firstname"):
            updates["firstname"] = first_name
        if last_name and not ep.get("lastname"):
            updates["lastname"] = last_name
        if company and not ep.get("company"):
            updates["company"] = company

        if updates:
            _update_contact(client, contact_id, updates)
            return {"status": "Aggiornato", "email": email_addr, "contact_id": contact_id}
        return {"status": "Ignorato", "email": email_addr, "contact_id": contact_id}

    # Build properties for new contact
    props: dict = {
        "email": email_addr,
        "hs_lead_source": "OFFLINE",           # closest standard HubSpot source
        "lead_source_detail": "Gmail",         # custom detail if property exists
    }
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if company:
        props["company"] = company

    contact_id = _create_contact(client, props)
    if not contact_id:
        return {"status": "Errore", "email": email_addr, "contact_id": None}

    # Add timeline note with "Inbound Gmail" tag
    _add_note(client, contact_id, "Inbound Gmail — contatto acquisito da email in arrivo")

    return {"status": "Creato", "email": email_addr, "contact_id": contact_id}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(poll_interval: int = 60):
    logger.info("=== Gmail → HubSpot Contact Sync avviato ===")
    logger.info("Intervallo polling: %ds", poll_interval)

    gmail = _get_gmail_service()
    hs = _get_hubspot_client()
    state = _load_state()
    processed: set = set(state.get("processed_ids", []))

    while True:
        try:
            message_ids, new_history_id = _fetch_new_message_ids(
                gmail, state.get("last_history_id")
            )
            new_ids = [mid for mid in message_ids if mid not in processed]

            if new_ids:
                logger.info("Elaborazione di %d nuova/e email...", len(new_ids))

            for msg_id in new_ids:
                try:
                    from_header = _get_from_header(gmail, msg_id)
                    if not from_header:
                        processed.add(msg_id)
                        continue

                    email_addr, name, domain = _parse_from_header(from_header)

                    if not email_addr or "@" not in email_addr:
                        processed.add(msg_id)
                        continue

                    if _SKIP_PATTERNS.search(email_addr):
                        logger.info("Ignorato (automatico): %s", email_addr)
                        processed.add(msg_id)
                        continue

                    result = _sync_sender(hs, email_addr, name, domain)

                    logger.info(
                        "%-12s | %-40s | ID: %s",
                        result["status"],
                        result["email"],
                        result["contact_id"] or "—",
                    )

                    processed.add(msg_id)

                except Exception as exc:
                    logger.error("Errore sul messaggio %s: %s", msg_id, exc)

            state["last_history_id"] = new_history_id
            state["processed_ids"] = list(processed)[-2000:]  # cap memory
            _save_state(state)

        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc)

        time.sleep(poll_interval)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL", "60")),
        help="Secondi tra un polling e l'altro (default: 60)",
    )
    args = parser.parse_args()
    run(poll_interval=args.interval)
