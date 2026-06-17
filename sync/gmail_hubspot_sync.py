"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs sender contacts to HubSpot.
Avoids duplicates by using email as unique key.

Requirements:
  pip install google-auth google-auth-oauthlib google-auth-httplib2
              google-api-python-client hubspot-api-client python-dotenv

Environment variables (.env):
  HUBSPOT_ACCESS_TOKEN   – HubSpot private app token
  GMAIL_CREDENTIALS_JSON – path to Google OAuth2 credentials JSON
  GMAIL_TOKEN_JSON       – path to token cache file (auto-created)
  DAYS_BACK              – how many days back to scan (default: 1)
  SKIP_SENDERS           – comma-separated emails to ignore (self, daemons…)
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_JSON", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_JSON", "token.json")
DAYS_BACK = int(os.getenv("DAYS_BACK", "1"))

_DEFAULT_SKIP = {
    "mailer-daemon@googlemail.com",
    "noreply@google.com",
    "no-reply@accounts.google.com",
}
_env_skip = {e.strip().lower() for e in os.getenv("SKIP_SENDERS", "").split(",") if e.strip()}
SKIP_SENDERS: set[str] = _DEFAULT_SKIP | _env_skip

# ── Gmail ─────────────────────────────────────────────────────────────────────


def gmail_service():
    creds: Optional[Credentials] = None
    token_path = Path(TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def list_inbox_senders(service, days_back: int = 1) -> list[dict]:
    """Return list of {email, name, subject, date} dicts for inbox messages."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y/%m/%d")
    query = f"in:inbox after:{cutoff} -from:me -in:draft"

    senders: dict[str, dict] = {}
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])

        for msg_stub in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_stub["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            raw_from = headers.get("From", "")
            name, email = parseaddr(raw_from)
            email = email.lower().strip()

            if not email or email in SKIP_SENDERS:
                continue
            if email in senders:
                continue  # already seen this sender

            senders[email] = {
                "email": email,
                "name": name.strip(),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            }

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return list(senders.values())


# ── Name / company parsing ────────────────────────────────────────────────────

_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "libero.it", "hotmail.com",
    "outlook.com", "icloud.com", "virgilio.it", "tiscali.it",
}

_ROLE_PREFIXES = {
    "ufficio", "ufficiostampa", "redazione", "press", "info", "comunicazione",
    "segreteria", "galleria", "notification", "noreply", "no-reply",
    "postmaster", "mailer",
}


def parse_name(display_name: str, email: str) -> tuple[str, str]:
    """Return (firstname, lastname) parsed from display name or email local part."""
    if display_name:
        parts = display_name.strip().split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        if len(parts) == 1:
            return parts[0], ""

    local = email.split("@")[0]
    # patterns like "firstname.lastname" or "f.lastname"
    if "." in local:
        parts = local.split(".")
        if len(parts) == 2 and parts[0].replace("-", "").isalpha() and parts[1].replace("-", "").isalpha():
            return parts[0].capitalize(), parts[1].capitalize()
    return local, ""


def company_from_domain(email: str) -> str:
    """Derive a company name from the email domain when not a generic provider."""
    domain = email.split("@")[-1].lower()
    if domain in _GENERIC_DOMAINS:
        return ""
    # strip common suffixes
    name = domain.split(".")[0]
    # capitalise each word (handles hyphenated names)
    return " ".join(w.capitalize() for w in re.split(r"[-_]", name))


def build_contact_props(sender: dict) -> dict:
    email = sender["email"]
    display_name = sender["name"]

    firstname, lastname = parse_name(display_name, email)
    company = company_from_domain(email)

    props: dict = {
        "email": email,
        "firstname": firstname,
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    return props


# ── HubSpot ───────────────────────────────────────────────────────────────────


def get_hubspot_client() -> HubSpot:
    return HubSpot(access_token=HUBSPOT_TOKEN)


def find_existing_contact(hs: HubSpot, email: str) -> Optional[dict]:
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.results:
        return resp.results[0]
    return None


def create_contact(hs: HubSpot, props: dict) -> dict:
    obj = SimplePublicObjectInputForCreate(properties=props)
    return hs.crm.contacts.basic_api.create(simple_public_object_input_for_create=obj)


def update_contact(hs: HubSpot, contact_id: str, props: dict):
    from hubspot.crm.contacts import SimplePublicObjectInput

    obj = SimplePublicObjectInput(properties=props)
    hs.crm.contacts.basic_api.update(
        contact_id=contact_id, simple_public_object_input=obj
    )


def sync_contact(hs: HubSpot, sender: dict) -> dict:
    """
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "id": ...}
    """
    email = sender["email"]
    desired = build_contact_props(sender)
    existing = find_existing_contact(hs, email)

    if existing is None:
        created = create_contact(hs, desired)
        return {"status": "Creato", "email": email, "id": created.id}

    # compute fields that are missing / empty in existing record
    ex_props = existing.properties
    updates = {
        k: v
        for k, v in desired.items()
        if k != "email" and not ex_props.get(k)
    }

    if updates:
        update_contact(hs, existing.id, updates)
        return {"status": "Aggiornato", "email": email, "id": existing.id}

    return {"status": "Ignorato", "email": email, "id": existing.id}


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Avvio sync Gmail → HubSpot (ultimi {DAYS_BACK} giorni)")

    svc = gmail_service()
    hs = get_hubspot_client()

    senders = list_inbox_senders(svc, days_back=DAYS_BACK)
    print(f"  Mittenti unici trovati: {len(senders)}")

    results = {"Creato": [], "Aggiornato": [], "Ignorato": []}

    for sender in senders:
        try:
            outcome = sync_contact(hs, sender)
            results[outcome["status"]].append(outcome)
            print(f"  [{outcome['status']:10s}] {outcome['email']}  (ID {outcome['id']})")
        except ApiException as exc:
            print(f"  [ERRORE] {sender['email']}: {exc}", file=sys.stderr)

    print(
        f"\nRiepilogo: Creati={len(results['Creato'])} | "
        f"Aggiornati={len(results['Aggiornato'])} | "
        f"Ignorati={len(results['Ignorato'])}"
    )
    return results


if __name__ == "__main__":
    main()
