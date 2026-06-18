#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for incoming emails, extracts sender contacts,
and syncs them to HubSpot avoiding duplicates.

Usage:
    python gmail_hubspot_sync.py

Environment variables:
    HUBSPOT_ACCESS_TOKEN   HubSpot Private App token (required)
    GMAIL_CREDENTIALS_FILE Path to OAuth credentials JSON (default: credentials.json)
"""

import os
import re
import json
import logging
from pathlib import Path
from typing import Optional, Tuple, NamedTuple
from email.utils import parseaddr

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.contacts.exceptions import ApiException

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("sync_state.json")

# Domains / addresses that belong to automated systems — never add these as contacts
_SKIP_DOMAINS = frozenset({
    "facebookmail.com", "googlemail.com", "bounce",
    "noreply", "no-reply", "donotreply",
})
_SKIP_EMAILS = frozenset({
    "mailer-daemon@googlemail.com",
    "pageupdates@facebookmail.com",
    "notification@priority.facebookmail.com",
})

# Email addresses that forward others' messages — look inside the snippet instead
_FORWARDER_EMAILS = frozenset({
    "redazione@latestata.it",
    "cristian.mameli.editore@gmail.com",
    "cristian.mameli@gmail.com",
})

# Personal / generic domains where the domain name tells us nothing about the company
_PERSONAL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.it", "libero.it", "hotmail.com",
    "outlook.com", "virgilio.it", "tiscali.it", "alice.it",
    "aol.com", "icloud.com", "me.com",
})


class Contact(NamedTuple):
    email: str
    firstname: str
    lastname: str
    company: str


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_message_ids": []}


def save_state(state: dict) -> None:
    # Keep at most 10 000 processed IDs to bound file size
    state["processed_message_ids"] = state["processed_message_ids"][-10_000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    token_path = Path("token.json")
    creds_path = Path(os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"))
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, query: str = "in:inbox -from:me newer_than:2d", max_results: int = 200) -> list:
    """Return a list of message dicts with id, sender, subject, snippet."""
    page_token = None
    messages = []

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": min(max_results - len(messages), 500)}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()

        for m in resp.get("messages", []):
            detail = service.users().messages().get(
                userId="me", messageId=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            messages.append({
                "id": m["id"],
                "sender": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": detail.get("snippet", ""),
            })

        page_token = resp.get("nextPageToken")
        if not page_token or len(messages) >= max_results:
            break

    return messages


# ---------------------------------------------------------------------------
# Contact extraction
# ---------------------------------------------------------------------------

def _should_skip(email: str) -> bool:
    if email in _SKIP_EMAILS:
        return True
    domain = email.split("@")[-1].lower()
    return any(s in domain for s in _SKIP_DOMAINS)


def _extract_forwarded_sender(snippet: str) -> Optional[Tuple[str, str]]:
    """
    Parse 'Da "Name" email@domain' or 'Da email@domain' from Italian forwarded snippets.
    Returns (email, name) or None.
    """
    # With quoted name
    m = re.search(
        r'Da\s+"?([^"<\n@]{1,80}?)"?\s*<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
        snippet,
    )
    if m:
        name = m.group(1).strip().rstrip('"').strip()
        email = m.group(2).strip().lower()
        return email, name

    # Without name
    m = re.search(r'Da\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', snippet)
    if m:
        return m.group(1).strip().lower(), ""

    return None


def _company_from_domain(domain: str) -> str:
    """Derive a company name from the email domain (best-effort)."""
    if domain in _PERSONAL_DOMAINS:
        return ""
    parts = domain.split(".")
    # Drop TLD (and ccSLD like "comune.sanseverinomarche.mc.it" → keep meaningful part)
    core = parts[-2] if len(parts) >= 2 else parts[0]
    return core.replace("-", " ").replace("_", " ").title()


def extract_contact(sender: str, snippet: str = "") -> Optional[Contact]:
    """
    Build a Contact from a raw RFC-2822 sender field and an optional snippet.
    Returns None if the sender should be skipped.
    """
    name, email = parseaddr(sender)
    email = email.lower().strip()

    if not email or "@" not in email:
        return None

    # For forwarder accounts, attempt to recover the original sender from the snippet
    if email in _FORWARDER_EMAILS and snippet:
        result = _extract_forwarded_sender(snippet)
        if result:
            fw_email, fw_name = result
            if not _should_skip(fw_email):
                email, name = fw_email, fw_name

    if _should_skip(email):
        return None

    domain = email.split("@")[-1].lower()
    company = _company_from_domain(domain)

    parts = name.strip().split()
    if len(parts) >= 2:
        firstname, lastname = parts[0], " ".join(parts[1:])
    elif parts:
        firstname, lastname = parts[0], ""
    else:
        firstname = email.split("@")[0].replace(".", " ").replace("_", " ").title()
        lastname = ""

    return Contact(email=email, firstname=firstname, lastname=lastname, company=company)


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def hs_find_contact(client, email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    try:
        req = PublicObjectSearchRequest(
            filter_groups=[FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])],
            properties=["email", "firstname", "lastname", "company", "leadsource"],
        )
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        return resp.results[0].to_dict() if resp.results else None
    except ApiException as exc:
        log.error("HubSpot search failed for %s: %s", email, exc)
        return None


def hs_create_contact(client, contact: Contact) -> Optional[str]:
    """Create a new HubSpot contact. Returns the new ID or None."""
    props = {
        "email": contact.email,
        "firstname": contact.firstname,
        "lastname": contact.lastname,
        "leadsource": "Gmail",
    }
    if contact.company:
        props["company"] = contact.company
    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        return result.id
    except ApiException as exc:
        log.error("HubSpot create failed for %s: %s", contact.email, exc)
        return None


def hs_update_contact(client, contact_id: str, contact: Contact, existing: dict) -> bool:
    """Fill in missing fields on an existing HubSpot contact. Returns True if any update was made."""
    ep = existing.get("properties", {})
    updates: dict = {}

    if not ep.get("firstname") and contact.firstname:
        updates["firstname"] = contact.firstname
    if not ep.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname
    if not ep.get("company") and contact.company:
        updates["company"] = contact.company
    if not ep.get("leadsource"):
        updates["leadsource"] = "Gmail"

    if not updates:
        return False

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update failed for id %s: %s", contact_id, exc)
        return False


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def sync(gmail_service, hs_client, state: dict) -> list[dict]:
    """
    Fetch recent inbox messages, extract unique sender contacts,
    and create/update them in HubSpot.

    Returns a list of result dicts with keys: stato, email, id_hubspot.
    """
    messages = fetch_inbox_messages(gmail_service)
    processed_ids = set(state.get("processed_message_ids", []))
    seen_emails: set = set()
    results: list = []

    for msg in messages:
        if msg["id"] in processed_ids:
            continue
        processed_ids.add(msg["id"])

        contact = extract_contact(msg["sender"], msg["snippet"])
        if not contact or contact.email in seen_emails:
            continue
        seen_emails.add(contact.email)

        existing = hs_find_contact(hs_client, contact.email)
        if existing:
            contact_id = existing["id"]
            updated = hs_update_contact(hs_client, contact_id, contact, existing)
            status = "Aggiornato" if updated else "Ignorato"
        else:
            contact_id = hs_create_contact(hs_client, contact)
            status = "Creato" if contact_id else "Errore"

        results.append({"stato": status, "email": contact.email, "id_hubspot": contact_id or "N/A"})
        log.info("[%s] %s — HubSpot ID: %s", status, contact.email, contact_id or "N/A")

    state["processed_message_ids"] = list(processed_ids)
    return results


def main() -> None:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN environment variable is required")

    state = load_state()
    gmail_service = get_gmail_service()
    hs_client = hubspot.Client.create(access_token=token)

    results = sync(gmail_service, hs_client, state)
    save_state(state)

    divider = "=" * 64
    print(f"\n{divider}")
    print(f"Sync completato — {len(results)} contatti processati")
    print(divider)

    for r in results:
        print(f"[{r['stato']:10}]  {r['email']:42}  ID: {r['id_hubspot']}")

    created = sum(1 for r in results if r["stato"] == "Creato")
    updated = sum(1 for r in results if r["stato"] == "Aggiornato")
    ignored = sum(1 for r in results if r["stato"] == "Ignorato")
    errors  = sum(1 for r in results if r["stato"] == "Errore")
    print(f"\n  Creati: {created}  Aggiornati: {updated}  Ignorati: {ignored}  Errori: {errors}")


if __name__ == "__main__":
    main()
