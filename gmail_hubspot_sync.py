"""
Gmail → HubSpot Contact Sync
----------------------------
Polls Gmail for new inbound emails, extracts sender data, and upserts
contacts in HubSpot.  Runs continuously; state is persisted in state.json
so each message is processed exactly once.

Required env vars (see .env.example):
  GMAIL_CREDENTIALS_FILE  – path to Google OAuth2 client_secret JSON
  HUBSPOT_ACCESS_TOKEN    – HubSpot private-app token
  POLL_INTERVAL_SECONDS   – seconds between Gmail polls (default: 60)
  STATE_FILE              – where to store last-seen history ID (default: state.json)
  SELF_EMAIL              – your own Gmail address (to skip outbound messages)
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
SELF_EMAIL = os.getenv("SELF_EMAIL", "").lower()

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

# Domains that carry no company signal
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "protonmail.com", "live.com", "aol.com",
    "mail.com", "zoho.com",
}


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    message_id: str = ""
    subject: str = ""
    date: str = ""


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    """Return an authenticated Gmail API service, refreshing/creating tokens."""
    creds: Optional[Credentials] = None

    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def get_new_messages(service, last_history_id: Optional[str]) -> tuple[list[dict], str]:
    """
    Returns (messages, new_history_id).
    Falls back to listing recent INBOX messages when no history ID is stored.
    """
    if last_history_id:
        try:
            resp = (
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
            new_id = resp.get("historyId", last_history_id)
            messages = []
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    messages.append(added["message"])
            return messages, new_id
        except Exception as exc:
            log.warning("History fetch failed (%s); falling back to list.", exc)

    # No history ID yet – get the current historyId and process nothing
    profile = service.users().getProfile(userId="me").execute()
    return [], profile["historyId"]


def fetch_message(service, msg_id: str) -> Optional[dict]:
    try:
        return (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
    except Exception as exc:
        log.warning("Could not fetch message %s: %s", msg_id, exc)
        return None


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _split_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last); handles single-word names."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def _company_from_domain(email: str) -> str:
    """Derive a company name from the email domain, skipping generic providers."""
    try:
        domain = email.split("@", 1)[1].lower()
        if domain in _GENERIC_DOMAINS:
            return ""
        # Strip common TLD suffixes and capitalise: acme.com → Acme
        name = domain.split(".")[0]
        return name.capitalize()
    except IndexError:
        return ""


def extract_sender(message: dict) -> Optional[SenderInfo]:
    headers = message.get("payload", {}).get("headers", [])
    raw_from = _header(headers, "From")
    if not raw_from:
        return None

    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.strip().lower()
    if not email_addr or "@" not in email_addr:
        return None

    # Skip emails we sent ourselves
    if SELF_EMAIL and email_addr == SELF_EMAIL:
        return None

    first, last = _split_name(display_name) if display_name else ("", "")
    company = _company_from_domain(email_addr)

    return SenderInfo(
        email=email_addr,
        first_name=first,
        last_name=last,
        company=company,
        message_id=message.get("id", ""),
        subject=_header(headers, "Subject"),
        date=_header(headers, "Date"),
    )


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def search_contact_by_email(email: str) -> Optional[dict]:
    """Return the first matching HubSpot contact or None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=HUBSPOT_HEADERS,
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def _build_properties(sender: SenderInfo, existing: Optional[dict] = None) -> dict:
    """
    Build the HubSpot property dict to write.
    When updating, only send fields that are currently blank in HubSpot.
    """
    existing_props = (existing or {}).get("properties", {})

    def _keep(field_name: str, new_val: str) -> Optional[str]:
        """Return new_val only if there is something to write and the field is empty."""
        if not new_val:
            return None
        if existing and existing_props.get(field_name):
            return None  # already filled – do not overwrite
        return new_val

    props: dict = {}
    if not existing:
        # Always set email on create
        props["email"] = sender.email

    if (v := _keep("firstname", sender.first_name)):
        props["firstname"] = v
    if (v := _keep("lastname", sender.last_name)):
        props["lastname"] = v
    if (v := _keep("company", sender.company)):
        props["company"] = v

    # Source / tag – set on create, leave alone on update
    if not existing:
        props["hs_lead_status"] = "NEW"
        props["leadsource"] = "Gmail"

    return props


def create_contact(sender: SenderInfo) -> dict:
    props = _build_properties(sender)
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=HUBSPOT_HEADERS,
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, sender: SenderInfo, existing: dict) -> dict:
    props = _build_properties(sender, existing)
    if not props:
        return existing  # nothing to update
    resp = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=HUBSPOT_HEADERS,
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def add_timeline_activity(contact_id: str, sender: SenderInfo) -> None:
    """Create a note on the contact recording the inbound email."""
    body = (
        f"Inbound Gmail received\n"
        f"Subject: {sender.subject or '(no subject)'}\n"
        f"Date: {sender.date}\n"
        f"Tag: Inbound Gmail"
    )
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(int(time.time() * 1000)),
        },
        "associations": [
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
    }
    try:
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/notes",
            headers=HUBSPOT_HEADERS,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Could not create timeline note for %s: %s", contact_id, exc)


# ── Upsert logic ──────────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "skipped"
    email: str
    contact_id: str = ""
    reason: str = ""


def upsert_contact(sender: SenderInfo) -> SyncResult:
    existing = search_contact_by_email(sender.email)

    if existing:
        contact_id = existing["id"]
        updated = update_contact(contact_id, sender, existing)
        updated_id = updated.get("id", contact_id)

        props_written = _build_properties(sender, existing)
        if props_written:
            add_timeline_activity(updated_id, sender)
            return SyncResult("updated", sender.email, updated_id)
        else:
            return SyncResult(
                "skipped", sender.email, contact_id, "all fields already present"
            )
    else:
        created = create_contact(sender)
        contact_id = created["id"]
        add_timeline_activity(contact_id, sender)
        return SyncResult("created", sender.email, contact_id)


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Main loop ─────────────────────────────────────────────────────────────────

def validate_config() -> bool:
    ok = True
    if not HUBSPOT_ACCESS_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN is not set.")
        ok = False
    if not Path(GMAIL_CREDENTIALS_FILE).exists():
        log.error("GMAIL_CREDENTIALS_FILE not found: %s", GMAIL_CREDENTIALS_FILE)
        ok = False
    return ok


def run_once(service, state: dict) -> dict:
    """Process one poll cycle.  Mutates and returns state."""
    last_history_id = state.get("last_history_id")
    messages, new_history_id = get_new_messages(service, last_history_id)

    if not messages:
        log.debug("No new messages (historyId=%s).", new_history_id)
        state["last_history_id"] = new_history_id
        return state

    log.info("Processing %d new message(s).", len(messages))

    for msg_stub in messages:
        full_msg = fetch_message(service, msg_stub["id"])
        if not full_msg:
            continue

        sender = extract_sender(full_msg)
        if not sender:
            continue

        try:
            result = upsert_contact(sender)
        except requests.HTTPError as exc:
            log.error("HubSpot error for %s: %s", sender.email, exc.response.text)
            continue
        except Exception as exc:
            log.error("Unexpected error for %s: %s", sender.email, exc)
            continue

        icon = {"created": "✚", "updated": "↺", "skipped": "─"}.get(result.status, "?")
        log.info(
            "%s  %-10s  %-40s  id=%s  %s",
            icon, result.status.upper(), result.email, result.contact_id,
            f"({result.reason})" if result.reason else "",
        )

    state["last_history_id"] = new_history_id
    return state


def main() -> None:
    if not validate_config():
        raise SystemExit(1)

    log.info("Authenticating with Gmail…")
    service = get_gmail_service()
    log.info("Gmail authenticated.  Polling every %ds.  Press Ctrl+C to stop.", POLL_INTERVAL)

    state = load_state()

    while True:
        try:
            state = run_once(service, state)
            save_state(state)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.error("Poll cycle error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
