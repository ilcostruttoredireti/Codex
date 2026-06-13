#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs senders as contacts in HubSpot.
"""

import os
import json
import re
import logging
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Gmail ─────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

# Domains / local-part patterns we never want to sync
_SKIP_DOMAINS = {
    "facebookmail.com", "facebook.com", "twitter.com", "linkedin.com",
    "notifications.google.com", "accounts.google.com", "mail.google.com",
    "bounce.com", "amazonses.com", "sendgrid.net", "mailchimp.com",
    "list-manage.com", "mailjet.com",
}
_SKIP_LOCAL_PATTERNS = re.compile(
    r"^(noreply|no-reply|donotreply|notification|notifications|bounce|"
    r"mailer-daemon|postmaster|admin|support|info|newsletter|unsubscribe)$"
)


def _gmail_credentials() -> Credentials:
    token_path = Path(os.getenv("GMAIL_TOKEN", "token.json"))
    creds_path = Path(os.getenv("GMAIL_CREDENTIALS", "credentials.json"))

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

    return creds


def get_gmail_service():
    return build("gmail", "v1", credentials=_gmail_credentials())


# ── HubSpot ───────────────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    key = os.getenv("HUBSPOT_API_KEY") or os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not key:
        raise EnvironmentError("Set HUBSPOT_API_KEY (or HUBSPOT_ACCESS_TOKEN) in .env")
    return hubspot.Client.create(access_token=key)


# ── Helpers ───────────────────────────────────────────────────────────────────

_FROM_RE = re.compile(r'^"?([^"<]*)"?\s*<([^>]+)>$')


def parse_sender(raw: str) -> tuple[str, str]:
    """Return (full_name, email) from a From header value."""
    raw = raw.strip()
    m = _FROM_RE.match(raw)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return "", raw.lower()


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def domain_to_company(domain: str) -> str:
    parts = domain.split(".")
    core = parts[-2] if len(parts) >= 2 else parts[0]
    return core.replace("-", " ").replace("_", " ").title()


def should_skip(email: str, user_email: str) -> bool:
    if not email or "@" not in email:
        return True
    if email.lower() == user_email.lower():
        return True
    local, domain = email.lower().split("@", 1)
    if domain in _SKIP_DOMAINS:
        return True
    if _SKIP_LOCAL_PATTERNS.match(local):
        return True
    return False


# ── State (processed message IDs) ────────────────────────────────────────────

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))


def load_state() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(processed: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(processed)))


# ── HubSpot contact operations ────────────────────────────────────────────────

def hs_find_contact(client: hubspot.Client, email: str):
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[
            Filter(property_name="email", operator="EQ", value=email)
        ])],
        properties=["email", "firstname", "lastname", "company", "hs_additional_emails"],
        limit=1,
    )
    res = client.crm.contacts.search_api.do_search(req)
    return res.results[0] if res.total > 0 else None


def hs_create_contact(client: hubspot.Client, email: str, first: str, last: str, company: str):
    props: dict[str, str] = {
        "email": email,
        "hs_lead_status": "NEW",
    }
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company
    # Source label
    props["hs_analytics_source"] = "OTHER"
    props["hs_analytics_source_data_1"] = "Gmail"

    return client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
    )


def hs_update_contact(client: hubspot.Client, contact_id: str, updates: dict[str, str]):
    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )


def hs_add_note(client: hubspot.Client, contact_id: str, email: str, subject: str):
    """Log an inbound email activity note on the contact."""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body = f"Email ricevuta da: {email}\nOggetto: {subject}\nFonte: Gmail"
    note = {
        "hs_timestamp": now_ms,
        "hs_note_body": body,
        "hs_attachment_ids": "",
    }
    try:
        result = client.crm.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=note)
        )
        # Associate note → contact
        client.crm.associations.v4.basic_api.create(
            object_type="notes",
            object_id=result.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_spec=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        )
    except Exception as e:
        log.warning("Could not create note for %s: %s", contact_id, e)


# ── Main sync loop ────────────────────────────────────────────────────────────

def sync(max_messages: int = 500, add_notes: bool = True) -> list[dict]:
    gmail = get_gmail_service()
    hs = get_hubspot_client()

    profile = gmail.users().getProfile(userId="me").execute()
    user_email: str = profile["emailAddress"]
    log.info("Gmail account: %s", user_email)

    processed = load_state()
    results: list[dict] = []

    page_token = None
    fetched = 0

    while fetched < max_messages:
        kw: dict = {"userId": "me", "q": "in:inbox", "maxResults": min(100, max_messages - fetched)}
        if page_token:
            kw["pageToken"] = page_token

        resp = gmail.users().messages().list(**kw).execute()
        messages = resp.get("messages", [])
        fetched += len(messages)

        for ref in messages:
            msg_id: str = ref["id"]

            if msg_id in processed:
                continue

            # Fetch only headers (faster than full message)
            msg = gmail.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["From", "Subject"]
            ).execute()

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            subject = headers.get("Subject", "(no subject)")

            name, email = parse_sender(raw_from)

            if should_skip(email, user_email):
                processed.add(msg_id)
                continue

            domain = email.split("@", 1)[1] if "@" in email else ""
            first, last = split_name(name)
            company = domain_to_company(domain) if domain else ""

            record = {"email": email, "msg_id": msg_id}

            try:
                existing = hs_find_contact(hs, email)

                if existing:
                    props = existing.properties
                    updates: dict[str, str] = {}
                    if not props.get("firstname") and first:
                        updates["firstname"] = first
                    if not props.get("lastname") and last:
                        updates["lastname"] = last
                    if not props.get("company") and company:
                        updates["company"] = company

                    if updates:
                        hs_update_contact(hs, existing.id, updates)
                        status = "Aggiornato"
                    else:
                        status = "Ignorato"

                    record["hubspot_id"] = existing.id

                    if add_notes and status != "Ignorato":
                        hs_add_note(hs, existing.id, email, subject)
                else:
                    new = hs_create_contact(hs, email, first, last, company)
                    status = "Creato"
                    record["hubspot_id"] = new.id

                    if add_notes:
                        hs_add_note(hs, new.id, email, subject)

            except ApiException as e:
                status = "Errore"
                record["error"] = str(e)
                log.error("HubSpot API error for %s: %s", email, e)

            record["stato"] = status
            results.append(record)
            processed.add(msg_id)

            log.info("[%-10s] %-45s  HubSpot ID: %s", status, email, record.get("hubspot_id", "—"))

        save_state(processed)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    _print_summary(results)
    return results


def _print_summary(results: list[dict]) -> None:
    creati = sum(1 for r in results if r["stato"] == "Creato")
    aggiornati = sum(1 for r in results if r["stato"] == "Aggiornato")
    ignorati = sum(1 for r in results if r["stato"] == "Ignorato")
    errori = sum(1 for r in results if r["stato"] == "Errore")

    log.info("─" * 60)
    log.info("Riepilogo: Creati=%d  Aggiornati=%d  Ignorati=%d  Errori=%d",
             creati, aggiornati, ignorati, errori)
    log.info("─" * 60)


if __name__ == "__main__":
    sync()
