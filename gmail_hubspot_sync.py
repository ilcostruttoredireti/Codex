#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Reads recent inbox messages, extracts unique external senders and syncs them
to HubSpot as contacts.  Designed to run as a scheduled routine (e.g. daily).

Usage:
    python gmail_hubspot_sync.py [--days N]

Output (per contact):
    Stato | Email | HubSpot ID

Dependencies:
    google-api-python-client, google-auth-httplib2, google-auth-oauthlib, hubspot-api-client
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

# ── constants ──────────────────────────────────────────────────────────────────

SKIP_DOMAINS: set[str] = {
    "googlemail.com",
    "google.com",
    "facebookmail.com",
    "facebook.com",
    "twitter.com",
    "linkedin.com",
    "accounts.google.com",
    "bounce.facebookmail.com",
}

SKIP_ADDRESSES: set[str] = {
    "mailer-daemon@googlemail.com",
    "postmaster@gmail.com",
    "noreply@gmail.com",
    "no-reply@gmail.com",
}

LEAD_SOURCE_VALUE = "EMAIL_MARKETING"
LEAD_SOURCE_DRILL_DOWN = "Gmail - Inbound"
INBOUND_TAG = "Inbound Gmail"

# ── helpers ────────────────────────────────────────────────────────────────────

def extract_domain(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else ""


def company_from_domain(domain: str) -> str:
    """Turn 'moonsrl.it' → 'moonsrl', 'bibliotecaarezzo.it' → 'bibliotecaarezzo'."""
    parts = domain.split(".")
    # Drop common TLDs and second-level generic parts
    generic = {"gmail", "yahoo", "hotmail", "outlook", "libero", "virgilio", "alice", "pec"}
    name = parts[0] if parts else ""
    return "" if name in generic else name.replace("-", " ").title()


def parse_sender(raw_sender: str) -> tuple[str, str, str]:
    """Return (display_name, email, domain)."""
    name, addr = parseaddr(raw_sender)
    addr = addr.lower().strip()
    domain = extract_domain(addr)
    return name.strip(), addr, domain


def split_name(display_name: str) -> tuple[str, str]:
    """Best-effort split of 'First Last' into (firstname, lastname)."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def is_skippable(email: str, domain: str, own_emails: set[str]) -> bool:
    if not email or "@" not in email:
        return True
    if email in SKIP_ADDRESSES:
        return True
    if domain in SKIP_DOMAINS:
        return True
    if email in own_emails:
        return True
    # Automated senders: notification@, noreply@, no-reply@, bounce@, …
    local = email.split("@")[0]
    if re.match(r"^(no.?reply|notification|bounce|mailer.daemon|postmaster|support|info\+)", local, re.I):
        return True
    return False


# ── Gmail access ───────────────────────────────────────────────────────────────

def get_gmail_service():
    """Build a Gmail API service using OAuth credentials from env / file."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("Install dependencies: pip install google-api-python-client google-auth-oauthlib")

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = None
    token_path = os.environ.get("GMAIL_TOKEN_PATH", "token.json")
    creds_path = os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_inbox_senders(service, days: int = 7, own_emails: set[str] | None = None) -> list[dict]:
    """Return list of {name, email, domain} dicts for unique external senders."""
    own_emails = own_emails or set()
    query = f"in:inbox -from:me newer_than:{days}d"

    seen: dict[str, dict] = {}
    page_token = None

    while True:
        kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])

        for msg in messages:
            headers_resp = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From"],
            ).execute()
            from_header = next(
                (h["value"] for h in headers_resp.get("payload", {}).get("headers", [])
                 if h["name"] == "From"),
                "",
            )
            name, addr, domain = parse_sender(from_header)
            if is_skippable(addr, domain, own_emails):
                continue
            if addr not in seen:
                seen[addr] = {"name": name, "email": addr, "domain": domain}

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return list(seen.values())


# ── HubSpot access ────────────────────────────────────────────────────────────

def get_hubspot_client():
    try:
        from hubspot import HubSpot
    except ImportError:
        sys.exit("Install dependencies: pip install hubspot-api-client")

    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        sys.exit("Set the HUBSPOT_ACCESS_TOKEN environment variable.")
    return HubSpot(access_token=token)


def find_contact(client, email: str) -> dict | None:
    """Return existing HubSpot contact or None."""
    from hubspot.crm.contacts import ApiException

    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": ["email", "firstname", "lastname", "company",
                               "hs_analytics_source", "hs_analytics_source_data_1"],
                "limit": 1,
            }
        )
        if resp.total > 0:
            return resp.results[0]
    except ApiException:
        pass
    return None


def build_contact_properties(
    sender: dict, existing: dict | None = None
) -> dict[str, str]:
    """Return only the property fields that are missing / need updating."""
    name = sender["name"]
    domain = sender["domain"]

    firstname, lastname = split_name(name) if name else ("", "")
    company = company_from_domain(domain)

    props: dict[str, str] = {}

    if existing:
        ep = existing.properties if hasattr(existing, "properties") else existing.get("properties", {})
        if not ep.get("firstname") and firstname:
            props["firstname"] = firstname
        if not ep.get("lastname") and lastname:
            props["lastname"] = lastname
        if not ep.get("company") and company:
            props["company"] = company
        if not ep.get("hs_analytics_source"):
            props["hs_analytics_source"] = LEAD_SOURCE_VALUE
            props["hs_analytics_source_data_1"] = LEAD_SOURCE_DRILL_DOWN
    else:
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        props["email"] = sender["email"]
        if company:
            props["company"] = company
        props["hs_analytics_source"] = LEAD_SOURCE_VALUE
        props["hs_analytics_source_data_1"] = LEAD_SOURCE_DRILL_DOWN

    return props


def sync_contact(client, sender: dict) -> tuple[str, str]:
    """
    Return (status, hubspot_id) where status is one of:
        CREATO | AGGIORNATO | IGNORATO
    """
    from hubspot.crm.contacts import ApiException, SimplePublicObjectInput

    email = sender["email"]
    existing = find_contact(client, email)

    if existing:
        contact_id = str(existing.id if hasattr(existing, "id") else existing.get("id"))
        updates = build_contact_properties(sender, existing)
        if updates:
            try:
                client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input=SimplePublicObjectInput(properties=updates),
                )
                return "AGGIORNATO", contact_id
            except ApiException as exc:
                return f"ERRORE({exc.status})", contact_id
        return "IGNORATO", contact_id

    # Create new contact
    props = build_contact_properties(sender)
    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create={"properties": props}
        )
        return "CREATO", str(result.id)
    except ApiException as exc:
        return f"ERRORE({exc.status})", ""


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts.")
    parser.add_argument("--days", type=int, default=7, help="Look back N days in inbox (default 7)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change without writing")
    args = parser.parse_args()

    print(f"[{datetime.now(timezone.utc).isoformat()}] Starting Gmail → HubSpot sync (last {args.days} days)")

    gmail = get_gmail_service()
    profile = gmail.users().getProfile(userId="me").execute()
    own_email = profile.get("emailAddress", "").lower()
    own_emails = {own_email} if own_email else set()

    print(f"Gmail account: {own_email}")
    senders = get_inbox_senders(gmail, days=args.days, own_emails=own_emails)
    print(f"Unique external senders found: {len(senders)}")

    if not senders:
        print("Nothing to sync.")
        return

    client = get_hubspot_client()

    print()
    print(f"{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
    print("-" * 75)

    created = updated = ignored = errors = 0
    for sender in senders:
        if args.dry_run:
            print(f"{'DRY-RUN':<12} {sender['email']:<45} -")
            continue

        status, contact_id = sync_contact(client, sender)
        print(f"{status:<12} {sender['email']:<45} {contact_id}")

        if status == "CREATO":
            created += 1
        elif status == "AGGIORNATO":
            updated += 1
        elif status == "IGNORATO":
            ignored += 1
        else:
            errors += 1

    print()
    print(f"Riepilogo: CREATI={created} | AGGIORNATI={updated} | IGNORATI={ignored} | ERRORI={errors}")


if __name__ == "__main__":
    main()
