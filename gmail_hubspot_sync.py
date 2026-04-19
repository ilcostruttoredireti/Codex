#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Continuously monitors the Gmail inbox and upserts sender contacts into HubSpot.

Setup:
  1. Place credentials.json (Google OAuth) in this directory.
  2. Set HUBSPOT_ACCESS_TOKEN in .env or as an env var.
  3. Run once — a browser window opens to authorise Gmail access.
  4. Subsequent runs use the saved token.json and sync_state.json.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SEC", "60"))
STATE_FILE = Path("sync_state.json")
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")

PERSONAL_DOMAINS: frozenset = frozenset(
    [
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "icloud.com",
        "live.com",
        "msn.com",
        "aol.com",
        "protonmail.com",
        "pm.me",
        "yandex.com",
        "mail.com",
    ]
)

_SKIP_RE = re.compile(
    r"(noreply|no-reply|do-not-reply|donotreply|mailer-daemon|"
    r"postmaster|bounce[sd]?|notifications?|autoconfirm|"
    r"support@.*\.(com|io|net))$",
    re.IGNORECASE,
)

# HubSpot association type: Note → Contact (HUBSPOT_DEFINED id 202)
NOTE_TO_CONTACT_TYPE_ID = 202

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as fh:
            return json.load(fh)
    return {"history_id": None, "processed": []}


def _save_state(state: dict) -> None:
    with STATE_FILE.open("w") as fh:
        json.dump(state, fh)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------


def build_gmail_service():
    creds: Optional[Credentials] = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    "credentials.json not found. Download it from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with TOKEN_FILE.open("w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _get_message_metadata(service, msg_id: str) -> dict:
    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )


def fetch_new_messages(service, state: dict) -> tuple[list, str]:
    """Return (list of message dicts, updated history_id).

    First run: fetches emails from the last 24 h.
    Subsequent runs: uses Gmail History API — only new inbox messages.
    """
    messages: list = []
    history_id: Optional[str] = state.get("history_id")

    try:
        if history_id:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            new_hid: str = resp.get("historyId", history_id)
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    messages.append(_get_message_metadata(service, added["message"]["id"]))
        else:
            result = (
                service.users()
                .messages()
                .list(userId="me", q="in:inbox newer_than:1d", maxResults=100)
                .execute()
            )
            for item in result.get("messages", []):
                messages.append(_get_message_metadata(service, item["id"]))
            profile = service.users().getProfile(userId="me").execute()
            new_hid = profile.get("historyId", "")

    except HttpError as exc:
        logger.error("Gmail API error: %s", exc)
        new_hid = history_id or ""

    return messages, new_hid


# ---------------------------------------------------------------------------
# Sender parsing
# ---------------------------------------------------------------------------


def parse_sender(from_header: str) -> Optional[dict]:
    """Return sender dict or None if the address should be skipped."""
    from_header = from_header.strip()

    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', from_header)
    if match:
        display_name = match.group(1).strip()
        email = match.group(2).strip().lower()
    elif "@" in from_header:
        display_name = ""
        email = from_header.lower().strip()
    else:
        return None

    if not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return None

    local = email.split("@")[0]
    if _SKIP_RE.search(local) or _SKIP_RE.search(email):
        return None

    name_parts = display_name.split(None, 1)
    firstname = name_parts[0] if name_parts else ""
    lastname = name_parts[1] if len(name_parts) > 1 else ""

    domain = email.split("@", 1)[1]
    company = ""
    if domain and domain not in PERSONAL_DOMAINS:
        # e.g. "acmecorp.io" → "Acmecorp"
        company = domain.split(".")[0].capitalize()

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
        "display_name": display_name,
    }


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------


def build_hubspot_client() -> hubspot.Client:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable is required")
    return hubspot.Client.create(access_token=token)


def hs_find_contact(hs: hubspot.Client, email: str):
    """Search HubSpot by email; return the contact object or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0] if resp.total > 0 else None


def hs_create_contact(hs: hubspot.Client, sender: dict) -> Optional[str]:
    props: dict = {"email": sender["email"], "lifecyclestage": "lead"}
    if sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if sender["company"]:
        props["company"] = sender["company"]

    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except ApiException as exc:
        if exc.status == 409:
            # Race condition: created between search and create
            existing = hs_find_contact(hs, sender["email"])
            return existing.id if existing else None
        logger.error("HubSpot create error (%s): %s", sender["email"], exc)
        return None


def hs_update_contact(
    hs: hubspot.Client, contact_id: str, sender: dict, existing
) -> bool:
    """Patch only fields that are currently empty. Returns True if a patch was sent."""
    ep = existing.properties or {}
    props: dict = {}
    if not ep.get("firstname") and sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if not ep.get("lastname") and sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if not ep.get("company") and sender["company"]:
        props["company"] = sender["company"]
    if not props:
        return False

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return True
    except ApiException as exc:
        logger.error("HubSpot update error (%s): %s", contact_id, exc)
        return False


def hs_add_note(
    hs: hubspot.Client, contact_id: str, email: str, subject: str
) -> None:
    """Create a Note in HubSpot and associate it with the contact."""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body = (
        f"[Inbound Gmail]\n"
        f"Email ricevuta da: {email}\n"
        f"Oggetto: {subject}\n"
        f"Tag: Inbound Gmail"
    )
    try:
        note = hs.crm.objects.basic_api.create(
            object_type="notes",
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={"hs_note_body": body, "hs_timestamp": now_ms},
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": NOTE_TO_CONTACT_TYPE_ID,
                            }
                        ],
                    }
                ],
            ),
        )
        logger.debug("Nota creata: %s → contatto %s", note.id, contact_id)
    except Exception as exc:
        logger.warning("Impossibile creare nota per il contatto %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def process_message(hs: hubspot.Client, msg: dict) -> dict:
    """Upsert the sender of *msg* into HubSpot.

    Returns a result dict with keys: status, email, contact_id.
    status is one of: Creato | Aggiornato | Ignorato
    """
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    from_header = headers.get("From", "")
    subject = headers.get("Subject", "(nessun oggetto)")

    if not from_header:
        return {"status": "Ignorato", "email": "", "contact_id": None}

    sender = parse_sender(from_header)
    if not sender:
        return {"status": "Ignorato", "email": from_header, "contact_id": None}

    existing = hs_find_contact(hs, sender["email"])

    if existing:
        updated = hs_update_contact(hs, existing.id, sender, existing)
        hs_add_note(hs, existing.id, sender["email"], subject)
        return {
            "status": "Aggiornato" if updated else "Ignorato",
            "email": sender["email"],
            "contact_id": existing.id,
        }

    contact_id = hs_create_contact(hs, sender)
    if contact_id:
        hs_add_note(hs, contact_id, sender["email"], subject)
        return {"status": "Creato", "email": sender["email"], "contact_id": contact_id}

    return {"status": "Ignorato", "email": sender["email"], "contact_id": None}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_ICONS = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}


def main() -> None:
    logger.info("=== Gmail → HubSpot Sync avviato (poll ogni %ds) ===", POLL_INTERVAL)

    gmail = build_gmail_service()
    hs = build_hubspot_client()
    state = _load_state()
    processed: set = set(state.get("processed", []))

    while True:
        logger.info("Controllo nuove email in arrivo…")
        messages, new_hid = fetch_new_messages(gmail, state)
        state["history_id"] = new_hid

        new_msgs = [m for m in messages if m["id"] not in processed]

        if new_msgs:
            logger.info("%d nuova/e email da elaborare.", len(new_msgs))
            for msg in new_msgs:
                processed.add(msg["id"])
                result = process_message(hs, msg)
                icon = _ICONS.get(result["status"], "❓")
                logger.info(
                    "%s  %-10s | %-40s | ID HubSpot: %s",
                    icon,
                    result["status"],
                    result["email"] or "(nessuna email)",
                    result.get("contact_id") or "N/A",
                )
        else:
            logger.info("Nessuna nuova email.")

        # Bound in-memory set; persist last 5 000 IDs to survive restarts
        state["processed"] = list(processed)[-5000:]
        _save_state(state)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
