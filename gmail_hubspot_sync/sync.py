"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for incoming emails, extracts sender info,
and syncs contacts to HubSpot (create or update, no duplicates).

Usage:
    python sync.py [--days N]  # default: last 7 days

Requirements:
    pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client

Auth:
    - Gmail: OAuth2 credentials.json (from Google Cloud Console)
    - HubSpot: HUBSPOT_API_KEY env var (private app token)
"""

import os
import re
import json
import logging
import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = "token.json"
CREDENTIALS_PATH = "credentials.json"

# Domains to skip (own accounts, automated senders, free email providers w/o useful company info)
SKIP_SENDERS = {
    "mailer-daemon@googlemail.com",
    "mailer-daemon@yahoo.com",
    "noreply@google.com",
    "no-reply@google.com",
}
SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "bounce.google.com",
}
# Own email accounts (configured via env or hardcoded)
OWN_EMAILS = set(
    e.strip()
    for e in os.getenv("OWN_EMAILS", "").split(",")
    if e.strip()
)

# Free email providers — domain is not a company domain
FREE_EMAIL_PROVIDERS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "libero.it", "virgilio.it", "alice.it",
    "tin.it", "icloud.com", "me.com", "tiscali.it", "fastwebnet.it",
}


@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    subjects: list = field(default_factory=list)

    def domain_company(self) -> str:
        """Derive company name from domain when not available."""
        if not self.domain or self.domain in FREE_EMAIL_PROVIDERS:
            return ""
        parts = self.domain.split(".")
        # Drop TLD(s) and clean up
        if len(parts) >= 2:
            name = parts[-2]
            # Handle subdomains: take the meaningful part
            if len(parts) >= 3 and parts[-3] not in ("www", "mail", "smtp"):
                name = parts[-3]
            return name.replace("-", " ").replace("_", " ").title()
        return self.domain


@dataclass
class SyncResult:
    email: str
    status: str  # "CREATO" | "AGGIORNATO" | "IGNORATO"
    hubspot_id: str = ""
    reason: str = ""

    def __str__(self):
        parts = [f"[{self.status}] {self.email}"]
        if self.hubspot_id:
            parts.append(f"ID:{self.hubspot_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " | ".join(parts)


# ─── Gmail ────────────────────────────────────────────────────────────────────

def gmail_auth() -> object:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _parse_sender(raw_from: str) -> tuple[str, str, str]:
    """Return (display_name, firstname, lastname, email)."""
    display_name, email = parseaddr(raw_from)
    email = email.lower().strip()
    firstname, lastname = "", ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        firstname = parts[0] if parts else ""
        lastname = parts[1] if len(parts) > 1 else ""
    return email, firstname, lastname


def fetch_senders(service, days: int = 7) -> dict[str, SenderContact]:
    """Fetch unique inbound senders from Gmail inbox over the last N days."""
    query = f"in:inbox -from:me newer_than:{days}d"
    contacts: dict[str, SenderContact] = {}
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().threads().list(**kwargs).execute()
        threads = resp.get("threads", [])

        for t in threads:
            thread = service.users().threads().get(
                userId="me", id=t["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()

            for msg in thread.get("messages", []):
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                raw_from = headers.get("From", "")
                subject = headers.get("Subject", "")

                email, firstname, lastname = _parse_sender(raw_from)
                if not email or "@" not in email:
                    continue

                domain = email.split("@")[1]

                if email in SKIP_SENDERS or email in OWN_EMAILS:
                    continue
                if any(email.endswith("@" + d) or domain == d for d in SKIP_DOMAINS):
                    continue

                if email not in contacts:
                    contacts[email] = SenderContact(
                        email=email,
                        firstname=firstname,
                        lastname=lastname,
                        domain=domain,
                    )
                else:
                    # Prefer non-empty name
                    c = contacts[email]
                    if not c.firstname and firstname:
                        c.firstname = firstname
                    if not c.lastname and lastname:
                        c.lastname = lastname

                contacts[email].subjects.append(subject)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Found %d unique senders in last %d days", len(contacts), days)
    return contacts


# ─── HubSpot ──────────────────────────────────────────────────────────────────

def hubspot_client() -> hubspot.Client:
    token = os.environ.get("HUBSPOT_API_KEY")
    if not token:
        raise RuntimeError("HUBSPOT_API_KEY environment variable not set")
    return hubspot.Client.create(access_token=token)


def find_contact(hs: hubspot.Client, email: str) -> dict | None:
    """Search HubSpot for a contact by email. Returns properties dict or None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
        ],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    result = hs.crm.contacts.search_api.do_search(public_object_search_request=search_req)
    if result.total > 0:
        r = result.results[0]
        return {"id": r.id, **r.properties}
    return None


def build_properties(sender: SenderContact, existing: dict | None) -> dict:
    """Build the property dict to set (only missing/empty fields)."""
    props = {}

    def _set_if_missing(key: str, value: str):
        if value and (existing is None or not existing.get(key)):
            props[key] = value

    _set_if_missing("firstname", sender.firstname)
    _set_if_missing("lastname", sender.lastname)

    company = sender.company or sender.domain_company()
    _set_if_missing("company", company)

    # Mark source as Gmail only if analytics source is not already set
    # Note: hs_analytics_source_data_1 is read-only in HubSpot
    if existing is None or not existing.get("hs_analytics_source"):
        props["hs_analytics_source"] = "OFFLINE"

    return props


def add_note(hs: hubspot.Client, contact_id: str, sender: SenderContact):
    """Add a timeline note recording the inbound Gmail email."""
    subjects = "; ".join(sender.subjects[:3]) or "(nessun oggetto)"
    body = (
        f"📧 Email ricevuta via Gmail\n"
        f"Mittente: {sender.email}\n"
        f"Oggetti: {subjects}\n"
        f"Fonte: Inbound Gmail\n"
        f"Data sync: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=hubspot.crm.objects.notes.SimplePublicObjectInputForCreate(
                properties={"hs_note_body": body, "hs_timestamp": str(ts)},
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                    }
                ],
            )
        )
    except Exception as e:
        log.warning("Errore creazione nota per %s: %s", sender.email, e)


def upsert_contact(hs: hubspot.Client, sender: SenderContact) -> SyncResult:
    existing = find_contact(hs, sender.email)
    props = build_properties(sender, existing)

    if existing is None:
        # CREATE
        create_props = {"email": sender.email, **props}
        try:
            created = hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=create_props
                )
            )
            log.info("CREATO %s (ID %s)", sender.email, created.id)
            add_note(hs, str(created.id), sender)
            return SyncResult(sender.email, "CREATO", str(created.id))
        except ApiException as e:
            log.error("Errore creazione %s: %s", sender.email, e)
            return SyncResult(sender.email, "IGNORATO", reason=str(e))
    else:
        # UPDATE — only if there's something to change
        if not props:
            return SyncResult(sender.email, "IGNORATO", str(existing["id"]), "nessun campo mancante")
        try:
            hs.crm.contacts.basic_api.update(
                contact_id=existing["id"],
                simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
                    properties=props
                ),
            )
            log.info("AGGIORNATO %s (ID %s) props=%s", sender.email, existing["id"], list(props))
            add_note(hs, str(existing["id"]), sender)
            return SyncResult(sender.email, "AGGIORNATO", str(existing["id"]))
        except ApiException as e:
            log.error("Errore aggiornamento %s: %s", sender.email, e)
            return SyncResult(sender.email, "IGNORATO", str(existing["id"]), str(e))


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(days: int = 7) -> list[SyncResult]:
    gmail = gmail_auth()
    hs = hubspot_client()

    senders = fetch_senders(gmail, days=days)
    results: list[SyncResult] = []

    for email, sender in senders.items():
        result = upsert_contact(hs, sender)
        results.append(result)

    # Summary
    created = sum(1 for r in results if r.status == "CREATO")
    updated = sum(1 for r in results if r.status == "AGGIORNATO")
    ignored = sum(1 for r in results if r.status == "IGNORATO")
    log.info("Sync completato — CREATI:%d AGGIORNATI:%d IGNORATI:%d", created, updated, ignored)

    print(f"\n{'='*60}")
    print(f"SYNC GMAIL → HUBSPOT  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")
    print(f"Creati: {created}  |  Aggiornati: {updated}  |  Ignorati: {ignored}")
    print(f"{'='*60}")
    for r in results:
        print(r)
    print()

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument("--days", type=int, default=7, help="Quanti giorni di email analizzare (default: 7)")
    args = parser.parse_args()
    run(days=args.days)
