#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for incoming emails, extracts sender contacts,
and syncs them to HubSpot CRM avoiding duplicates.

Setup:
  pip install -r requirements.txt
  Set HUBSPOT_API_KEY environment variable.
  Place Gmail credentials.json (OAuth2) in the working directory.
  On first run, a browser window opens to authorize Gmail access.
"""

import os
import re
import base64
from datetime import datetime, timedelta
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest


GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Email domains whose local-part should not be treated as personal contacts
EXCLUDED_DOMAINS = {
    "facebookmail.com",
    "bounce.mail.google.com",
    "accounts.google.com",
    "amazonses.com",
    "sendgrid.net",
    "mailchimp.com",
    "exacttarget.com",
    "sparkpostmail.com",
}

# Local-part prefixes that signal automated/bulk mail
EXCLUDED_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "postmaster", "bounce", "newsletter", "notifications",
    "notification", "unsubscribe",
)

# Domains treated as personal (company name not inferred from them)
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "libero.it", "virgilio.it", "tiscali.it", "icloud.com",
    "live.com", "protonmail.com", "fastmail.com",
}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_hubspot_client():
    api_key = os.environ["HUBSPOT_API_KEY"]
    return hubspot.Client.create(access_token=api_key)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Return (email, firstname, lastname) from a raw From header."""
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', from_header.strip())
    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
        parts = name.split()
        firstname = parts[0] if parts else ""
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
        return email, firstname, lastname
    email = from_header.strip().lower()
    return email, "", ""


def should_skip(email: str) -> bool:
    if "@" not in email:
        return True
    local, domain = email.rsplit("@", 1)
    if domain in EXCLUDED_DOMAINS:
        return True
    if local.lower().startswith(EXCLUDED_LOCAL_PREFIXES):
        return True
    return False


def company_from_domain(email: str, fallback_name: str = "") -> str:
    """Derive a company name from the email domain, or fall back to the sender name."""
    domain = email.rsplit("@", 1)[-1].lower()
    if domain in PERSONAL_DOMAINS:
        return fallback_name
    parts = domain.split(".")
    # take the second-level domain label
    label = parts[-2] if len(parts) >= 2 else parts[0]
    return label.replace("-", " ").title()


def extract_forwarded_sender(body: str) -> Optional[tuple[str, str, str]]:
    """
    Extract the original sender from a forwarded email body.
    Handles both Italian ('Da:') and English ('From:') headers.
    """
    patterns = [
        # Italian: Da "Name" email or Da: "Name" <email>
        r'Da\s*:\s*"([^"]+)"\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
        r'Da\s*"([^"]+)"\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        r'Da\s*:\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
        # English
        r'From\s*:\s*"([^"]+)"\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
        r'From\s*:\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    ]
    for pattern in patterns:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                name_raw, email = groups[0].strip(), groups[1].strip().lower()
            else:
                name_raw, email = "", groups[0].strip().lower()
            parts = name_raw.split()
            firstname = parts[0] if parts else ""
            lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
            if not should_skip(email):
                return email, firstname, lastname
    return None


def decode_body_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")


def get_plain_text(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return decode_body_part(payload)
    for part in payload.get("parts", []):
        text = get_plain_text(part)
        if text:
            return text
    return ""


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def fetch_inbox_messages(service, days: int = 1) -> list[dict]:
    after = (datetime.utcnow() - timedelta(days=days)).strftime("%Y/%m/%d")
    query = f"in:inbox -from:me after:{after}"
    resp = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    return resp.get("messages", [])


def get_message(service, msg_id: str) -> dict:
    return service.users().messages().get(userId="me", id=msg_id, format="full").execute()


def extract_contact_from_message(service, msg_meta: dict) -> Optional[tuple[str, str, str]]:
    """Return (email, firstname, lastname) for the relevant sender of a message."""
    msg = get_message(service, msg_meta["id"])
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    email, firstname, lastname = parse_sender(headers.get("From", ""))
    if should_skip(email):
        return None

    # Try to unwrap forwarded content to find the original external sender
    body = get_plain_text(msg.get("payload", {}))
    if body:
        forwarded = extract_forwarded_sender(body)
        if forwarded:
            return forwarded

    return email, firstname, lastname


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------

def find_contact(client, email: str) -> Optional[object]:
    search_req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=search_req)
        return resp.results[0] if resp.total > 0 else None
    except ApiException as exc:
        print(f"    HubSpot search error: {exc}")
        return None


def create_contact(client, email: str, firstname: str, lastname: str, company: str) -> Optional[str]:
    props = {"email": email, "hs_lead_source": "OTHER"}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    try:
        resp = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        return resp.id
    except ApiException as exc:
        print(f"    HubSpot create error: {exc}")
        return None


def update_contact(client, contact_id: str, updates: dict) -> bool:
    if not updates:
        return False
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        print(f"    HubSpot update error: {exc}")
        return False


def sync_contact(client, email: str, firstname: str, lastname: str, company: str) -> tuple[str, str]:
    """Return (status, hubspot_id). Status: CREATO | AGGIORNATO | IGNORATO."""
    existing = find_contact(client, email)
    if existing:
        props = existing.properties
        updates = {}
        if firstname and not props.get("firstname"):
            updates["firstname"] = firstname
        if lastname and not props.get("lastname"):
            updates["lastname"] = lastname
        if company and not props.get("company"):
            updates["company"] = company
        if updates:
            ok = update_contact(client, existing.id, updates)
            return ("AGGIORNATO" if ok else "IGNORATO"), existing.id
        return "IGNORATO", existing.id

    contact_id = create_contact(client, email, firstname, lastname, company)
    return ("CREATO" if contact_id else "IGNORATO"), (contact_id or "")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(days: int = 1):
    print(f"\n{'='*70}")
    print(f"  Gmail → HubSpot Sync  [{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}]")
    print(f"{'='*70}\n")

    gmail = get_gmail_service()
    hs = get_hubspot_client()

    messages = fetch_inbox_messages(gmail, days=days)
    print(f"  {len(messages)} messaggi trovati in inbox (ultimi {days}g)\n")
    print(f"  {'STATO':<12} {'EMAIL':<45} {'HUBSPOT ID'}")
    print(f"  {'─'*68}")

    seen: set[str] = set()
    results: list[dict] = []

    for meta in messages:
        contact = extract_contact_from_message(gmail, meta)
        if not contact:
            continue
        email, firstname, lastname = contact
        if email in seen:
            continue
        seen.add(email)

        company = company_from_domain(email, f"{firstname} {lastname}".strip())
        status, contact_id = sync_contact(hs, email, firstname, lastname, company)

        icon = {"CREATO": "✓", "AGGIORNATO": "↑", "IGNORATO": "–"}.get(status, "?")
        print(f"  [{icon}] {status:<10} {email:<45} {contact_id}")
        results.append({"status": status, "email": email, "hubspot_id": contact_id})

    created  = sum(1 for r in results if r["status"] == "CREATO")
    updated  = sum(1 for r in results if r["status"] == "AGGIORNATO")
    ignored  = sum(1 for r in results if r["status"] == "IGNORATO")

    print(f"\n  {'─'*68}")
    print(f"  Totale: {len(results)}  |  Creati: {created}  |  Aggiornati: {updated}  |  Ignorati: {ignored}")
    print(f"{'='*70}\n")
    return results


if __name__ == "__main__":
    import sys
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    main(days=days_back)
