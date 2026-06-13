#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for incoming emails, extracts sender contact info,
and syncs to HubSpot — creating new contacts or updating existing ones.

Setup:
    pip install -r requirements.txt
    cp .env.example .env        # fill in credentials
    python gmail_to_hubspot_sync.py

Scheduling (cron every 30 minutes):
    */30 * * * * cd /path/to/project && python gmail_to_hubspot_sync.py
"""

import os
import re
import json
import logging
import base64
import email.utils
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
import requests

# Gmail API (google-api-python-client + google-auth-oauthlib)
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "gmail_credentials.json")

PROCESSED_LABEL = "HubSpot-Synced"
PROCESSED_LABEL_COLOR = {"backgroundColor": "#16a766", "textColor": "#ffffff"}

HUBSPOT_API_BASE = "https://api.hubapi.com"

# Internal senders to never sync
SKIP_EMAILS = {
    "mailer-daemon@googlemail.com",
    "notification@priority.facebookmail.com",
    "analytics-noreply@google.com",
    "noreply@google.com",
    "no-reply@accounts.google.com",
    "pubblica.latestata@gmail.com",
    "cristian.mameli.editore@gmail.com",
    "redazione@latestata.it",
}

# Domains whose senders we ignore (notifications, bounce services, etc.)
SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "google.com",
    "amazonses.com",
    "sendgrid.net",
    "mailchimp.com",
    "mailer.mailchimp.com",
}

# Generic personal/webmail domains (no company derivation)
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "libero.it", "alice.it", "tiscali.it",
    "virgilio.it", "icloud.com", "me.com", "protonmail.com",
}

# Internal sender domains belonging to the publication itself
FORWARDING_SENDERS = {
    "redazione@latestata.it",
    "cristian.mameli.editore@gmail.com",
    "pubblica.latestata@gmail.com",
}


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate and return Gmail API service object."""
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, label_name: str) -> str:
    """Return Gmail label ID for label_name, creating the label if absent."""
    resp = service.users().labels().list(userId="me").execute()
    for label in resp.get("labels", []):
        if label["name"] == label_name:
            return label["id"]

    new_label = service.users().labels().create(
        userId="me",
        body={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
            "color": PROCESSED_LABEL_COLOR,
        },
    ).execute()
    log.info(f"Created Gmail label: {label_name}")
    return new_label["id"]


def get_message_body(msg_data: dict) -> str:
    """Extract plain-text body from a Gmail message dict."""
    payload = msg_data.get("payload", {})

    def _decode(data: str) -> str:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")

    # Direct body
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        return _decode(body_data)

    # Walk parts
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return _decode(data)
        # Nested parts (multipart/alternative inside multipart/mixed)
        for sub in part.get("parts", []):
            if sub.get("mimeType") == "text/plain":
                data = sub.get("body", {}).get("data", "")
                if data:
                    return _decode(data)

    return ""


# ── Contact extraction ────────────────────────────────────────────────────────

_FWD_SENDER_PATTERNS = [
    re.compile(r'(?:Da|From)\s*:\s*"?([^"<\n]+?)"?\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>', re.I),
    re.compile(r'(?:Da|From)\s*:\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', re.I),
]


def extract_forwarded_sender(text: str) -> tuple[Optional[str], Optional[str]]:
    """
    Try to extract the original sender (name, email) from a forwarded message
    body or snippet. Returns (name, email) or (None, None).
    """
    for pattern in _FWD_SENDER_PATTERNS:
        m = pattern.search(text)
        if m:
            if len(m.groups()) == 2:
                return m.group(1).strip(), m.group(2).strip().lower()
            return None, m.group(1).strip().lower()
    return None, None


def parse_sender(sender_str: str) -> tuple[Optional[str], Optional[str]]:
    """Parse a RFC 5322 From header into (display_name, email_address)."""
    name, addr = email.utils.parseaddr(sender_str)
    if not addr and "@" in sender_str:
        addr = sender_str.strip()
    return (name.strip() or None), (addr.lower().strip() or None)


def split_name(full_name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split a full name string into (first, last)."""
    if not full_name:
        return None, None
    parts = full_name.strip().split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else None)


def company_from_domain(email_addr: str) -> Optional[str]:
    """Derive a company name hint from the email domain (skips generic domains)."""
    if not email_addr or "@" not in email_addr:
        return None
    domain = email_addr.split("@")[1].lower()
    if domain in GENERIC_DOMAINS:
        return None
    # Strip TLD and ccTLD, capitalise the main label
    parts = domain.split(".")
    # For domains like comune.sanseverinomarche.mc.it take the meaningful part
    if len(parts) >= 2:
        label = parts[-2] if len(parts) == 2 else parts[-3] if parts[-2] in {"gov", "edu", "org", "com", "net"} else parts[-2]
        return label.replace("-", " ").title()
    return None


def should_skip(email_addr: str) -> bool:
    """Return True if this sender should be ignored."""
    if not email_addr or "@" not in email_addr:
        return True
    if email_addr in SKIP_EMAILS:
        return True
    domain = email_addr.split("@")[1].lower()
    if domain in SKIP_DOMAINS:
        return True
    local = email_addr.split("@")[0].lower()
    if any(kw in local for kw in ("noreply", "no-reply", "mailer-daemon", "bounce", "postmaster")):
        return True
    return False


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def search_contact(api_key: str, email_addr: str) -> Optional[dict]:
    """Search HubSpot for an existing contact by email. Returns contact dict or None."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email_addr}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(api_key), json=payload, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(api_key: str, props: dict) -> dict:
    """Create a new HubSpot contact with given properties."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
    resp = requests.post(url, headers=_hs_headers(api_key), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def update_contact(api_key: str, contact_id: str, props: dict) -> dict:
    """Patch an existing HubSpot contact."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_hs_headers(api_key), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def add_note(api_key: str, contact_id: str, body: str) -> None:
    """Add a timeline note (activity) to a HubSpot contact."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/notes"
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        }],
    }
    resp = requests.post(url, headers=_hs_headers(api_key), json=payload, timeout=15)
    resp.raise_for_status()


# ── Core logic ────────────────────────────────────────────────────────────────

def process_message(
    service,
    msg_data: dict,
    processed_label_id: str,
    hubspot_key: str,
    seen_emails: set,
) -> dict:
    """
    Process one Gmail message: extract sender, sync to HubSpot, label message.

    Returns dict with keys: status, email, contact_id, subject.
    """
    payload = msg_data.get("payload", {})
    headers_map = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    sender_str = headers_map.get("from", "")
    subject = headers_map.get("subject", "")
    snippet = msg_data.get("snippet", "")
    msg_id = msg_data["id"]

    name, email_addr = parse_sender(sender_str)

    # Unwrap forwarded emails: real sender is in the body / snippet
    is_forwarding_account = email_addr in FORWARDING_SENDERS
    is_fwd_subject = subject.strip().upper().startswith(("FW:", "FWD:", "I:"))
    if is_forwarding_account or is_fwd_subject:
        body_text = get_message_body(msg_data)
        fwd_name, fwd_email = extract_forwarded_sender(snippet + "\n" + body_text)
        if fwd_email:
            email_addr = fwd_email
            name = fwd_name or name

    if not email_addr or should_skip(email_addr):
        return {"status": "ignored", "email": email_addr or "—", "contact_id": None, "subject": subject}

    # Deduplicate within this run
    if email_addr in seen_emails:
        return {"status": "duplicate", "email": email_addr, "contact_id": None, "subject": subject}
    seen_emails.add(email_addr)

    first, last = split_name(name)
    company = company_from_domain(email_addr)

    note_body = (
        f"📨 Email inbound ricevuta via Gmail\n"
        f"Oggetto: {subject}\n"
        f"Data: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Tag: Inbound Gmail"
    )

    existing = search_contact(hubspot_key, email_addr)

    if existing:
        contact_id = existing["id"]
        ep = existing.get("properties", {})

        update_props: dict = {}
        if first and not ep.get("firstname"):
            update_props["firstname"] = first
        if last and not ep.get("lastname"):
            update_props["lastname"] = last
        if company and not ep.get("company"):
            update_props["company"] = company
        if ep.get("hs_analytics_source") != "EMAIL_MARKETING":
            update_props["hs_analytics_source"] = "EMAIL_MARKETING"

        if update_props:
            update_contact(hubspot_key, contact_id, update_props)

        add_note(hubspot_key, contact_id, note_body)
        status = "updated"
    else:
        props: dict = {"email": email_addr, "hs_analytics_source": "EMAIL_MARKETING"}
        if first:
            props["firstname"] = first
        if last:
            props["lastname"] = last
        if company:
            props["company"] = company

        result = create_contact(hubspot_key, props)
        contact_id = result["id"]
        add_note(hubspot_key, contact_id, note_body)
        status = "created"

    # Mark message as processed in Gmail
    try:
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"addLabelIds": [processed_label_id]},
        ).execute()
    except Exception as e:
        log.warning(f"Could not label message {msg_id}: {e}")

    log.info(f"[{status.upper()}] {email_addr} → ID {contact_id}")
    return {"status": status, "email": email_addr, "contact_id": contact_id, "subject": subject}


# ── Entry point ───────────────────────────────────────────────────────────────

def sync(query: str = f"in:inbox -label:{PROCESSED_LABEL}") -> list[dict]:
    """
    Fetch all inbox messages not yet labelled HubSpot-Synced and sync their
    senders to HubSpot.  Returns list of result dicts.
    """
    hubspot_key = os.getenv("HUBSPOT_API_KEY") or os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_key:
        raise ValueError("Set HUBSPOT_API_KEY (or HUBSPOT_ACCESS_TOKEN) in your .env file")

    service = get_gmail_service()
    label_id = get_or_create_label(service, PROCESSED_LABEL)

    results: list[dict] = []
    seen_emails: set[str] = set()
    page_token = None

    log.info(f"Starting Gmail → HubSpot sync (query: {query!r})")

    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token

        batch = service.users().messages().list(**kwargs).execute()
        messages = batch.get("messages", [])

        if not messages:
            break

        for msg_stub in messages:
            try:
                msg_data = service.users().messages().get(
                    userId="me", id=msg_stub["id"], format="full"
                ).execute()
                result = process_message(service, msg_data, label_id, hubspot_key, seen_emails)
                results.append(result)
            except Exception as exc:
                log.error(f"Error processing message {msg_stub['id']}: {exc}")

        page_token = batch.get("nextPageToken")
        if not page_token:
            break

    created = sum(1 for r in results if r["status"] == "created")
    updated = sum(1 for r in results if r["status"] == "updated")
    ignored = sum(1 for r in results if r["status"] in ("ignored", "duplicate"))
    log.info(f"Sync complete — created: {created} | updated: {updated} | ignored: {ignored}")

    return results


def print_report(results: list[dict]) -> None:
    actionable = [r for r in results if r["status"] in ("created", "updated")]
    if not actionable:
        print("Nessun nuovo contatto da sincronizzare.")
        return

    print("\n=== GMAIL → HUBSPOT SYNC REPORT ===")
    print(f"{'STATO':<10} {'EMAIL':<45} {'ID HUBSPOT'}")
    print("─" * 80)
    for r in actionable:
        print(f"{r['status'].upper():<10} {r['email']:<45} {r['contact_id'] or '—'}")

    created = sum(1 for r in results if r["status"] == "created")
    updated = sum(1 for r in results if r["status"] == "updated")
    ignored = sum(1 for r in results if r["status"] in ("ignored", "duplicate"))
    print(f"\nTOTALE  →  {created} creati | {updated} aggiornati | {ignored} ignorati")


if __name__ == "__main__":
    results = sync()
    print_report(results)
