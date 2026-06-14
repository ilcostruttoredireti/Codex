#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for new incoming emails, extracts sender data,
and creates or updates contacts in HubSpot. Uses email as the unique key.

Required environment variables:
    HUBSPOT_ACCESS_TOKEN   - HubSpot private app access token
    GMAIL_CREDENTIALS_FILE - Path to Gmail OAuth2 credentials JSON (default: credentials.json)
    GMAIL_TOKEN_FILE       - Path to stored Gmail token (default: gmail_token.json)
    OWNER_EMAIL            - Gmail account email address to skip self-emails
    SYNC_LOOKBACK_DAYS     - Days to look back on first run (default: 7)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

import hubspot
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from hubspot.crm.contacts import ApiException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

STATE_FILE = Path(os.getenv("STATE_FILE", ".gmail_sync_state.json"))
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json"))
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "").lower()
SYNC_LOOKBACK_DAYS = int(os.getenv("SYNC_LOOKBACK_DAYS", "7"))

SKIP_DOMAINS = frozenset(
    {
        "facebookmail.com",
        "googlemail.com",
        "bounces.google.com",
        "mailer-daemon.google.com",
        "notifications.google.com",
    }
)
SKIP_PREFIXES = frozenset(
    {"noreply", "no-reply", "donotreply", "notification", "mailer-daemon", "postmaster"}
)
GENERIC_EMAIL_DOMAINS = frozenset(
    {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "libero.it", "virgilio.it", "icloud.com"}
)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_sync_timestamp": None, "processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------


def build_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console and set GMAIL_CREDENTIALS_FILE."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def list_new_messages(service, after_timestamp: str | None) -> list[dict]:
    """Return inbox messages newer than after_timestamp (RFC 3339 or None)."""
    if after_timestamp:
        dt = datetime.fromisoformat(after_timestamp.replace("Z", "+00:00"))
        epoch = int(dt.timestamp())
        query = f"in:inbox -from:me after:{epoch}"
    else:
        days_ago = datetime.now(timezone.utc) - timedelta(days=SYNC_LOOKBACK_DAYS)
        epoch = int(days_ago.timestamp())
        query = f"in:inbox -from:me after:{epoch}"

    messages = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message_sender(service, message_id: str) -> dict | None:
    """Fetch the From header of a single message."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata", metadataHeaders=["From", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        date_header = headers.get("Date", "")
        return {"from": from_header, "date": date_header, "id": message_id}
    except HttpError as e:
        log.warning("Failed to fetch message %s: %s", message_id, e)
        return None


# ---------------------------------------------------------------------------
# Sender parsing
# ---------------------------------------------------------------------------


def parse_sender(from_header: str) -> tuple[str | None, str | None]:
    """Return (display_name, email) from a From header value."""
    name, email = parseaddr(from_header)
    email = email.strip().lower() if email else None
    name = name.strip() if name else None
    return name, email


def should_skip(email: str) -> bool:
    if not email:
        return True
    if OWNER_EMAIL and email == OWNER_EMAIL:
        return True
    domain = email.split("@")[-1] if "@" in email else ""
    prefix = email.split("@")[0] if "@" in email else ""
    if domain in SKIP_DOMAINS:
        return True
    if any(prefix.lower().startswith(p) for p in SKIP_PREFIXES):
        return True
    return False


def extract_name_parts(full_name: str | None) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None
    parts = full_name.strip().split(None, 1)
    firstname = parts[0] if parts else None
    lastname = parts[1] if len(parts) > 1 else None
    return firstname, lastname


def company_from_domain(email: str) -> str | None:
    domain = email.split("@")[-1] if "@" in email else ""
    if not domain or domain in GENERIC_EMAIL_DOMAINS:
        return None
    # e.g. 'gallerianazionalemarche.it' → 'Gallerianazionalemarche'
    # take the second-to-last segment (the SLD)
    parts = domain.split(".")
    name = parts[-2] if len(parts) >= 2 else parts[0]
    return name.replace("-", " ").title()


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------


def build_hubspot_client() -> hubspot.Client:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is not set.")
    return hubspot.Client.create(access_token=token)


def find_contact_by_email(client: hubspot.Client, email: str) -> dict | None:
    from hubspot.crm.contacts import PublicObjectSearchRequest

    filters = [{"propertyName": "email", "operator": "EQ", "value": email}]
    search_request = PublicObjectSearchRequest(
        filter_groups=[{"filters": filters}],
        properties=["email", "firstname", "lastname", "company", "lead_source"],
        limit=1,
    )
    try:
        result = client.crm.contacts.search_api.do_search(public_object_search_request=search_request)
        if result.results:
            return result.results[0]
    except ApiException as e:
        log.error("HubSpot search error for %s: %s", email, e)
    return None


def create_contact(client: hubspot.Client, props: dict) -> dict | None:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    body = SimplePublicObjectInputForCreate(properties=props)
    try:
        contact = client.crm.contacts.basic_api.create(simple_public_object_input_for_create=body)
        return contact
    except ApiException as e:
        if e.status == 409:
            # Already exists (race condition) — fetch and return
            log.debug("Contact %s already exists (409), fetching.", props.get("email"))
            return find_contact_by_email(client, props["email"])
        log.error("HubSpot create error for %s: %s", props.get("email"), e)
    return None


def update_contact(client: hubspot.Client, contact_id: str, props: dict) -> bool:
    from hubspot.crm.contacts import SimplePublicObjectInput

    body = SimplePublicObjectInput(properties=props)
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id, simple_public_object_input=body
        )
        return True
    except ApiException as e:
        log.error("HubSpot update error for contact %s: %s", contact_id, e)
    return False


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------


def build_contact_props(name: str | None, email: str) -> dict:
    firstname, lastname = extract_name_parts(name)
    company = company_from_domain(email)
    # hs_analytics_source tracks how the contact was acquired.
    # EMAIL_MARKETING is the correct HubSpot enum value for email-sourced contacts.
    # To store "Gmail" as a label, create a custom text property (e.g. "contact_source_detail")
    # in HubSpot Settings → Properties and set CUSTOM_SOURCE_PROPERTY below.
    props: dict = {"email": email, "hs_analytics_source": "EMAIL_MARKETING"}
    custom_prop = os.getenv("CUSTOM_SOURCE_PROPERTY")
    if custom_prop:
        props[custom_prop] = "Gmail"
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def fields_needing_update(existing: dict, desired: dict) -> dict:
    """Return only the fields from desired that are missing in existing."""
    existing_props = existing.properties if hasattr(existing, "properties") else existing
    updates = {}
    for key, val in desired.items():
        if key == "email":
            continue
        current = existing_props.get(key)
        if not current and val:
            updates[key] = val
    return updates


def sync_sender(client: hubspot.Client, name: str | None, email: str) -> dict:
    """Sync one sender to HubSpot. Returns a result dict."""
    result = {"email": email, "status": None, "hubspot_id": None}

    desired = build_contact_props(name, email)
    existing = find_contact_by_email(client, email)

    if existing:
        contact_id = existing.id if hasattr(existing, "id") else existing["id"]
        result["hubspot_id"] = contact_id
        updates = fields_needing_update(existing, desired)
        if updates:
            ok = update_contact(client, contact_id, updates)
            result["status"] = "Aggiornato" if ok else "Errore aggiornamento"
            log.info("AGGIORNATO  %s (ID %s) → %s", email, contact_id, updates)
        else:
            result["status"] = "Ignorato"
            log.info("IGNORATO    %s (ID %s) — nessun campo mancante", email, contact_id)
    else:
        contact = create_contact(client, desired)
        if contact:
            contact_id = contact.id if hasattr(contact, "id") else contact["id"]
            result["hubspot_id"] = contact_id
            result["status"] = "Creato"
            log.info("CREATO      %s (ID %s)", email, contact_id)
        else:
            result["status"] = "Errore creazione"

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    state = load_state()
    processed_ids: set[str] = set(state.get("processed_message_ids", []))

    gmail = build_gmail_service()
    hubspot_client = build_hubspot_client()

    messages = list_new_messages(gmail, state.get("last_sync_timestamp"))
    log.info("Trovati %d messaggi da esaminare.", len(messages))

    # Deduplicate senders within this batch
    seen_emails: dict[str, str | None] = {}  # email → display name
    new_ids: list[str] = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed_ids:
            continue
        new_ids.append(msg_id)
        info = get_message_sender(gmail, msg_id)
        if not info:
            continue
        name, email = parse_sender(info["from"])
        if not email or should_skip(email):
            continue
        if email not in seen_emails:
            seen_emails[email] = name

    log.info("Mittenti unici da elaborare: %d", len(seen_emails))

    results = []
    for email, name in seen_emails.items():
        res = sync_sender(hubspot_client, name, email)
        results.append(res)
        time.sleep(0.2)  # gentle rate limiting

    # Print summary
    print("\n--- RIEPILOGO SINCRONIZZAZIONE ---")
    print(f"{'Stato':<20} {'Email':<45} {'HubSpot ID'}")
    print("-" * 80)
    for r in results:
        print(f"{r['status']:<20} {r['email']:<45} {r['hubspot_id'] or '-'}")

    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    errors = sum(1 for r in results if "Errore" in (r["status"] or ""))
    print(f"\nCreati: {created}  Aggiornati: {updated}  Ignorati: {ignored}  Errori: {errors}")

    # Persist state
    processed_ids.update(new_ids)
    # Keep only last 5000 IDs to avoid unbounded growth
    state["processed_message_ids"] = list(processed_ids)[-5000:]
    state["last_sync_timestamp"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
