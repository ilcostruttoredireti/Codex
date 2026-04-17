#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot.

Usage:
    HUBSPOT_ACCESS_TOKEN=<token> python gmail_hubspot_sync.py

Required files:
    credentials.json  – Google OAuth2 client secrets (from Google Cloud Console)

On first run a browser window opens for Gmail OAuth consent.
Subsequent runs reuse the saved token.json.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")
STATE_FILE = Path(".processed_messages.json")

HUBSPOT_API_BASE = "https://api.hubapi.com"
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Domains that produce automated/transactional mail – skip them
_NOISE_PATTERNS = re.compile(
    r"(noreply|no-reply|mailer-daemon|bounce|notifications|"
    r"donotreply|do-not-reply|postmaster|support@google|"
    r"@googlemail\.com|@accounts\.google)",
    re.I,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail authentication ──────────────────────────────────────────────────────

def get_gmail_service():
    creds: Credentials | None = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    "credentials.json not found. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Processed-message state ───────────────────────────────────────────────────

def load_state() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(processed: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(processed)))


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def extract_sender(from_header: str) -> tuple[str, str, str]:
    """Parse 'Display Name <email@domain>' → (name, email, domain)."""
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()
    if not email or "@" not in email:
        return "", "", ""
    domain = email.split("@", 1)[1]
    name = display_name.strip().strip('"').strip("'")
    return name, email, domain


def split_name(display_name: str) -> tuple[str, str]:
    """'First Last' → ('First', 'Last'). Best-effort."""
    parts = display_name.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return display_name, ""


def company_from_domain(domain: str) -> str:
    """'acme.com' → 'Acme'."""
    label = domain.split(".")[0]
    return label.capitalize()


def fetch_new_inbox_messages(service, processed: set[str]) -> list[dict]:
    """Pull unprocessed INBOX messages (metadata only)."""
    resp = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=50,
    ).execute()

    new_msgs: list[dict] = []
    for m in resp.get("messages", []):
        if m["id"] in processed:
            continue
        detail = service.users().messages().get(
            userId="me",
            id=m["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        new_msgs.append(detail)

    return new_msgs


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> dict | None:
    """Return existing HubSpot contact or None."""
    payload = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": email},
        ]}],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    r = httpx.post(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    r = httpx.post(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    r = httpx.patch(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hs_add_note(contact_id: str, body: str) -> None:
    """Attach a timeline note to a contact."""
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": 202,   # Note → Contact
            }],
        }],
    }
    r = httpx.post(
        f"{HUBSPOT_API_BASE}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    if r.status_code not in (200, 201):
        log.warning("Timeline note failed for contact %s: %s", contact_id, r.text)


# ── Core sync logic ───────────────────────────────────────────────────────────

def sync_contact(
    name: str,
    email: str,
    domain: str,
    subject: str,
) -> tuple[str, str]:
    """
    Ensure the contact exists in HubSpot.
    Returns (status, contact_id)  where status ∈ {Created, Updated, Ignored}.
    """
    first, last = split_name(name)
    company = company_from_domain(domain)
    note_body = f"Inbound email received\nFrom: {email}\nSubject: {subject}\nTag: {CONTACT_TAG}"

    existing = hs_find_contact(email)

    if existing:
        contact_id = existing["id"]
        props = existing.get("properties", {})

        updates: dict[str, str] = {}
        if not props.get("firstname") and first:
            updates["firstname"] = first
        if not props.get("lastname") and last:
            updates["lastname"] = last
        if not props.get("company") and company:
            updates["company"] = company

        if updates:
            hs_update_contact(contact_id, updates)
            hs_add_note(contact_id, note_body)
            return "Updated", contact_id

        # Contact is complete – still log the inbound activity
        hs_add_note(contact_id, note_body)
        return "Ignored", contact_id

    # New contact
    new_props: dict[str, str] = {
        "email": email,
        "hs_lead_source": CONTACT_SOURCE,
    }
    if first:
        new_props["firstname"] = first
    if last:
        new_props["lastname"] = last
    if company:
        new_props["company"] = company

    created = hs_create_contact(new_props)
    contact_id = created["id"]
    hs_add_note(contact_id, note_body)
    return "Created", contact_id


# ── Message processor ─────────────────────────────────────────────────────────

def process_message(msg: dict) -> tuple[str, str, str] | None:
    """
    Process one Gmail message.
    Returns (status, email, contact_id) or None when the message should be skipped.
    """
    headers = msg.get("payload", {}).get("headers", [])
    from_header = _header(headers, "From")
    subject = _header(headers, "Subject") or "(no subject)"

    name, email, domain = extract_sender(from_header)
    if not email:
        return None

    if _NOISE_PATTERNS.search(email) or _NOISE_PATTERNS.search(domain):
        log.debug("Skipping noise sender: %s", email)
        return None

    try:
        status, contact_id = sync_contact(name, email, domain, subject)
    except httpx.HTTPStatusError as exc:
        log.error("HubSpot API error for %s: %s", email, exc.response.text)
        return None

    return status, email, contact_id


# ── Main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    if not HUBSPOT_TOKEN:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable is not set.")

    log.info("Gmail → HubSpot sync started  (poll interval: %ds)", POLL_INTERVAL)
    service = get_gmail_service()
    processed: set[str] = load_state()

    while True:
        try:
            new_messages = fetch_new_inbox_messages(service, processed)
            if new_messages:
                log.info("Found %d new message(s) to process.", len(new_messages))

            for msg in new_messages:
                msg_id = msg["id"]
                result = process_message(msg)
                processed.add(msg_id)

                if result:
                    status, email, contact_id = result
                    print(
                        f"[{status:8}]  {email:<42}  HubSpot ID: {contact_id}"
                    )
                else:
                    log.debug("Message %s skipped (noise / invalid sender).", msg_id)

            if new_messages:
                save_state(processed)

        except Exception:
            log.exception("Unexpected error during sync cycle.")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
