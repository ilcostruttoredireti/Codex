"""
Gmail → HubSpot Contact Sync
Monitors inbox continuously, syncs sender contacts to HubSpot.
"""

import email.utils
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import hubspot
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from hubspot.crm.contacts.exceptions import ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))        # seconds between polls
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
TOKEN_FILE = Path(os.getenv("TOKEN_FILE", "token.json"))
CREDENTIALS_FILE = Path(os.getenv("CREDENTIALS_FILE", "credentials.json"))
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN", "")

# Domains that belong to free email providers — company name is not derived from them.
_GENERIC_DOMAINS = {
    "gmail", "yahoo", "hotmail", "outlook", "live", "msn",
    "icloud", "me", "mac", "protonmail", "aol", "zoho",
    "yandex", "gmx", "mail", "inbox",
}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hs_sync")


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def build_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds: Optional[Credentials] = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Google OAuth credentials not found at {CREDENTIALS_FILE}. "
                    "Download them from Google Cloud Console and save as credentials.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_current_history_id(svc) -> str:
    """Return the inbox's current historyId (used as start-point on first run)."""
    profile = svc.users().getProfile(userId="me").execute()
    return str(profile["historyId"])


def fetch_new_message_ids(svc, last_history_id: str) -> tuple[list[str], str]:
    """
    Return (new_msg_ids, updated_history_id) since last_history_id.
    Only INBOX messages are considered.
    """
    new_ids: list[str] = []
    new_history_id = last_history_id

    try:
        resp = svc.users().history().list(
            userId="me",
            startHistoryId=last_history_id,
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()

        new_history_id = str(resp.get("historyId", last_history_id))

        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                labels = msg.get("labelIds", [])
                if "INBOX" in labels:
                    new_ids.append(msg["id"])

    except HttpError as exc:
        log.warning("Gmail History API error (%s); will retry next cycle.", exc)

    return new_ids, new_history_id


def fetch_from_header(svc, msg_id: str) -> Optional[str]:
    """Retrieve only the From header of a Gmail message."""
    try:
        msg = svc.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From"],
        ).execute()
        for h in msg["payload"]["headers"]:
            if h["name"].lower() == "from":
                return h["value"]
    except HttpError as exc:
        log.error("Failed to fetch message %s: %s", msg_id, exc)
    return None


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Parse a 'From' header into (email_addr, display_name, domain).
    Handles both 'Name <addr>' and bare 'addr' formats.
    """
    display_name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()
    domain = addr.split("@")[-1] if "@" in addr else ""
    return addr, display_name.strip(), domain


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def build_hs_client():
    if not HUBSPOT_TOKEN:
        raise EnvironmentError("HUBSPOT_TOKEN environment variable is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def hs_find_contact(hs, email_addr: str):
    """Return existing HubSpot contact object or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(
                property_name="email",
                operator="EQ",
                value=email_addr,
            )])
        ],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        result = hs.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        if result.total > 0:
            return result.results[0]
    except ApiException as exc:
        log.error("HubSpot search error: %s", exc)
    return None


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from an email domain."""
    if not domain:
        return ""
    base = domain.split(".")[0]
    if base in _GENERIC_DOMAINS:
        return ""
    return base.capitalize()


def build_contact_props(email_addr: str, name: str, domain: str) -> dict[str, str]:
    """Build HubSpot property dict from parsed sender data."""
    parts = name.split(maxsplit=1) if name else []
    props: dict[str, str] = {
        "email": email_addr,
        "leadsource": "Gmail",
    }
    if parts:
        props["firstname"] = parts[0]
    if len(parts) > 1:
        props["lastname"] = parts[1]
    company = _company_from_domain(domain)
    if company:
        props["company"] = company
    return props


def hs_create_contact(hs, props: dict[str, str]) -> Optional[str]:
    """Create a new HubSpot contact; return its ID or None on failure."""
    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except ApiException as exc:
        log.error("HubSpot create error: %s", exc)
        return None


def hs_update_contact(hs, contact_id: str, existing_props: dict, new_props: dict[str, str]) -> bool:
    """Patch only fields that are currently empty on the existing contact."""
    updates = {
        k: v
        for k, v in new_props.items()
        if k != "email" and not existing_props.get(k)
    }
    if not updates:
        return False
    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error: %s", exc)
        return False


def hs_add_note(hs, contact_id: str, email_addr: str) -> None:
    """Add an 'Inbound Gmail' note to the contact timeline (best-effort)."""
    try:
        from hubspot.crm.objects.notes.models import (
            SimplePublicObjectInputForCreate as NoteCreate,
        )
        ts_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        note = NoteCreate(
            properties={
                "hs_note_body": (
                    f"Inbound email received from {email_addr}.\n"
                    "Tag: Inbound Gmail\nSource: Gmail"
                ),
                "hs_timestamp": ts_ms,
            },
            associations=[{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 202,
                }],
            }],
        )
        hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not add note for contact %s: %s", contact_id, exc)


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"history_id": None, "processed": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Core processing ───────────────────────────────────────────────────────────

def process_message(gmail_svc, hs, msg_id: str, processed: set[str]) -> None:
    """Process one Gmail message: extract sender, sync to HubSpot, log result."""
    if msg_id in processed:
        return

    from_header = fetch_from_header(gmail_svc, msg_id)
    if not from_header:
        processed.add(msg_id)
        return

    email_addr, name, domain = parse_sender(from_header)
    if not email_addr or "@" not in email_addr:
        log.debug("Skipping message %s – unparseable sender: %r", msg_id, from_header)
        processed.add(msg_id)
        return

    props = build_contact_props(email_addr, name, domain)
    existing = hs_find_contact(hs, email_addr)

    if existing:
        existing_props = existing.properties or {}
        updated = hs_update_contact(hs, existing.id, existing_props, props)
        status = "Updated" if updated else "Ignored"
        contact_id = existing.id
    else:
        contact_id = hs_create_contact(hs, props)
        status = "Created" if contact_id else "Error"

    if contact_id:
        hs_add_note(hs, contact_id, email_addr)

    log.info(
        "[%-8s]  %-40s  HubSpot ID: %s",
        status,
        email_addr,
        contact_id or "N/A",
    )
    processed.add(msg_id)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    log.info("Gmail → HubSpot contact sync starting (poll interval: %ds)", POLL_INTERVAL)

    gmail_svc = build_gmail_service()
    hs = build_hs_client()
    state = load_state()

    # First run: anchor to current historyId, skip all existing messages.
    if state["history_id"] is None:
        state["history_id"] = get_current_history_id(gmail_svc)
        save_state(state)
        log.info("First run – anchored at historyId %s. Monitoring for new mail…", state["history_id"])

    while True:
        try:
            new_ids, new_history_id = fetch_new_message_ids(gmail_svc, state["history_id"])

            processed: set[str] = set(state.get("processed", []))

            if new_ids:
                log.info("Found %d new inbox message(s).", len(new_ids))
                for msg_id in new_ids:
                    process_message(gmail_svc, hs, msg_id, processed)
            else:
                log.debug("No new messages.")

            # Cap processed cache at 2 000 entries to bound file size.
            state["processed"] = list(processed)[-2000:]
            state["history_id"] = new_history_id
            save_state(state)

        except Exception:  # noqa: BLE001
            log.exception("Unexpected error in sync cycle – will retry next poll.")

        log.debug("Sleeping %ds…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
