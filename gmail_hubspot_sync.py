#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors the Gmail inbox, extracts sender contact data from both direct
and forwarded emails, and syncs them to HubSpot (create or update).

Usage (standalone with API keys):
    export HUBSPOT_API_KEY="your-private-app-token"
    export GMAIL_TOKEN_FILE="token.json"          # OAuth token after first auth
    export GMAIL_CREDENTIALS_FILE="credentials.json"  # Google Cloud OAuth client secret
    export LOOKBACK_DAYS="1"
    python gmail_hubspot_sync.py

This script can also be orchestrated by a scheduled Claude Code session
using the Gmail and HubSpot MCP tools (no API keys required in that mode).
"""

import base64
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HUBSPOT_API_KEY: str = os.environ.get("HUBSPOT_API_KEY", "")
GMAIL_TOKEN_FILE: str = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
GMAIL_CREDENTIALS_FILE: str = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
LOOKBACK_DAYS: int = int(os.environ.get("LOOKBACK_DAYS", "1"))

# Emails that belong to this account and should never be synced as contacts
OWN_EMAILS: set[str] = {
    "cristian.mameli.editore@gmail.com",
    "pubblica.latestata@gmail.com",
    "redazione@latestata.it",
}

# Addresses that forward incoming press releases to the inbox
FORWARDER_EMAILS: set[str] = {
    "redazione@latestata.it",
    "cristian.mameli.editore@gmail.com",
}

# Domains that are always automated / non-human
SKIP_DOMAINS: set[str] = {
    "facebookmail.com",
    "googlemail.com",
    "google.com",
    "accounts.google.com",
    "bounce.com",
}

# Email local-part prefixes that indicate automated senders
SKIP_PREFIXES: list[str] = [
    "no-reply",
    "noreply",
    "mailer-daemon",
    "postmaster",
    "notification",
    "notifications",
    "alert",
    "bounce",
    "donotreply",
    "do-not-reply",
    "support",
    "unsubscribe",
    "pa-certa",
]

# Generic freemail domains — company is not inferred from them
GENERIC_DOMAINS: set[str] = {
    "gmail.com",
    "yahoo.com",
    "yahoo.it",
    "hotmail.com",
    "hotmail.it",
    "outlook.com",
    "outlook.it",
    "libero.it",
    "alice.it",
    "tiscali.it",
    "virgilio.it",
    "icloud.com",
    "me.com",
    "live.com",
    "live.it",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    """Build an authenticated Gmail API client."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds: Optional[Credentials] = None

    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_inbox_messages(service, days: int = 1) -> list[dict]:
    """Return all inbox messages received in the last *days* days."""
    after = int((datetime.utcnow() - timedelta(days=days)).timestamp())
    query = f"in:inbox after:{after}"
    messages: list[dict] = []
    page_token: Optional[str] = None

    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return messages


def _decode_body_part(part: dict) -> str:
    """Recursively extract plain-text body from a Gmail message part."""
    if part.get("mimeType") == "text/plain":
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    for sub in part.get("parts", []):
        text = _decode_body_part(sub)
        if text:
            return text
    return ""


def get_message_details(service, msg_id: str) -> dict:
    """Fetch sender, subject, and plain-text body for a message."""
    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    body = _decode_body_part(msg.get("payload", {}))
    return {
        "id": msg_id,
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "body": body,
    }


# ---------------------------------------------------------------------------
# Contact extraction
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+%'-]+@[\w.-]+\.[a-zA-Z]{2,}")


def should_skip_email(email: str) -> bool:
    """Return True if the address should never become a HubSpot contact."""
    email = email.lower()
    if email in {e.lower() for e in OWN_EMAILS}:
        return True
    local, _, domain = email.partition("@")
    if domain in SKIP_DOMAINS:
        return True
    for prefix in SKIP_PREFIXES:
        if local.startswith(prefix):
            return True
    return False


def parse_name_from_header(sender: str) -> tuple[str, str]:
    """
    Split 'Firstname Lastname <email>' or '"Name" <email>' into (firstname, lastname).
    Returns ("", "") when no name is present.
    """
    match = re.match(r'^"?([^"<@\n]{2,})"?\s*<', sender)
    if match:
        parts = match.group(1).strip().split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        if parts:
            return parts[0], ""
    return "", ""


def extract_forwarded_sender(body: str) -> Optional[dict]:
    """
    Parse the first 2 000 characters of a forwarded email body and return
    a contact dict for the original sender, or None if nothing useful found.

    Handles Italian forwarding headers ('Da …') and English ones ('From …').
    """
    snippet = body[:2000]

    # Pattern A: Da "Name" email@domain  (Aruba / Webmail style)
    # Pattern B: Da: "Name" <email>      (standard forward)
    # Pattern C: From: "Name" <email>
    # Pattern D: Da: email (bare address)
    patterns = [
        (r'Da\s+"([^"]+)"\s+(' + _EMAIL_RE.pattern + r")", 2),
        (r'Da:\s*"?([^"<\n]{1,60})"?\s*<(' + _EMAIL_RE.pattern + r")>", 2),
        (r'From:\s*"?([^"<\n]{1,60})"?\s*<(' + _EMAIL_RE.pattern + r")>", 2),
        (r'Da:\s*(' + _EMAIL_RE.pattern + r")", 1),
        (r'From:\s*(' + _EMAIL_RE.pattern + r")", 1),
        # Forwarded message block (Gmail style): "Da: Name <email>"
        (r'Da:\s*([^<\n]{1,60})\s*<(' + _EMAIL_RE.pattern + r")>", 2),
    ]

    for pattern, n_groups in patterns:
        m = re.search(pattern, snippet, re.IGNORECASE)
        if m:
            if n_groups == 2:
                name_raw, email = m.group(1).strip(), m.group(2).strip().lower()
            else:
                name_raw, email = "", m.group(1).strip().lower()

            if not _EMAIL_RE.fullmatch(email):
                continue
            if should_skip_email(email):
                continue

            parts = name_raw.split()
            return {
                "email": email,
                "firstname": parts[0] if parts else "",
                "lastname": " ".join(parts[1:]) if len(parts) > 1 else "",
            }

    return None


def company_from_domain(email: str) -> str:
    """Derive a company name hint from a non-generic email domain."""
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if domain in GENERIC_DOMAINS:
        return ""
    parts = domain.split(".")
    # Drop trailing TLD and common country SLDs
    skip_slds = {"co", "com", "org", "net", "gov", "edu", "ac"}
    meaningful = [p for p in parts[:-1] if p not in skip_slds]
    if meaningful:
        return meaningful[-1].replace("-", " ").title()
    return ""


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

_HS_BASE = "https://api.hubapi.com"


def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}


def hubspot_search_contact(email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict for *email*, or None."""
    url = f"{_HS_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hubspot_create_contact(contact: dict) -> dict:
    """Create a new HubSpot contact and return the API response."""
    url = f"{_HS_BASE}/crm/v3/objects/contacts"
    props: dict = {
        "email": contact["email"],
        "hs_lead_status": "NEW",
        "leadsource": "Gmail",
    }
    for field in ("firstname", "lastname", "company"):
        if contact.get(field):
            props[field] = contact[field]
    resp = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def hubspot_update_contact(contact_id: str, updates: dict) -> None:
    """Patch a HubSpot contact with only the given *updates*."""
    url = f"{_HS_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": updates}, timeout=15)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_contact(contact_data: dict) -> dict:
    """
    Create or update a HubSpot contact for *contact_data*.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": …, "contact_id": …}.
    """
    email = contact_data["email"]
    existing = hubspot_search_contact(email)

    if existing:
        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates: dict = {}

        if contact_data.get("firstname") and not props.get("firstname"):
            updates["firstname"] = contact_data["firstname"]
        if contact_data.get("lastname") and not props.get("lastname"):
            updates["lastname"] = contact_data["lastname"]
        if contact_data.get("company") and not props.get("company"):
            updates["company"] = contact_data["company"]

        if updates:
            hubspot_update_contact(contact_id, updates)
            return {"status": "Aggiornato", "email": email, "contact_id": contact_id}
        return {"status": "Ignorato", "email": email, "contact_id": contact_id}

    # New contact
    if not contact_data.get("company"):
        contact_data["company"] = company_from_domain(email)
    created = hubspot_create_contact(contact_data)
    return {"status": "Creato", "email": email, "contact_id": created["id"]}


def run_sync(days: int = LOOKBACK_DAYS) -> list[dict]:
    """
    Fetch the last *days* days of inbox mail, extract unique contacts,
    sync each to HubSpot, and return the result list.
    """
    service = get_gmail_service()
    log.info("Fetching inbox for the last %d day(s)…", days)
    messages = get_inbox_messages(service, days)
    log.info("Found %d messages", len(messages))

    seen: set[str] = set()
    results: list[dict] = []

    for msg_ref in messages:
        try:
            details = get_message_details(service, msg_ref["id"])
            sender = details["sender"]
            body = details["body"]

            email_match = _EMAIL_RE.search(sender)
            if not email_match:
                continue
            sender_email = email_match.group(0).lower()

            contact_data: Optional[dict] = None

            if sender_email in {e.lower() for e in FORWARDER_EMAILS}:
                # Extract original press-contact from the forwarded body
                if body:
                    contact_data = extract_forwarded_sender(body)
            elif not should_skip_email(sender_email):
                firstname, lastname = parse_name_from_header(sender)
                contact_data = {
                    "email": sender_email,
                    "firstname": firstname,
                    "lastname": lastname,
                }

            if contact_data and contact_data["email"] not in seen:
                seen.add(contact_data["email"])
                log.info("Processing %s", contact_data["email"])
                result = process_contact(contact_data)
                results.append(result)
                log.info("  → %s (ID %s)", result["status"], result["contact_id"])

        except Exception as exc:
            log.error("Error on message %s: %s", msg_ref["id"], exc)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    results = run_sync()

    created  = [r for r in results if r["status"] == "Creato"]
    updated  = [r for r in results if r["status"] == "Aggiornato"]
    ignored  = [r for r in results if r["status"] == "Ignorato"]

    separator = "=" * 62
    print(f"\n{separator}")
    print("GMAIL → HUBSPOT SYNC — RISULTATI")
    print(separator)
    print(f"  ✓ Creati:      {len(created)}")
    print(f"  ↑ Aggiornati:  {len(updated)}")
    print(f"  - Ignorati:    {len(ignored)}")
    print(f"  Totale:        {len(results)}")
    print()
    print("  Stato        Email                                    ID HubSpot")
    print("  " + "-" * 60)
    for r in results:
        icon = {"Creato": "✓", "Aggiornato": "↑", "Ignorato": "-"}.get(r["status"], "?")
        print(f"  {icon} {r['status']:<12} {r['email']:<42} {r['contact_id']}")
    print(separator)


if __name__ == "__main__":
    main()
