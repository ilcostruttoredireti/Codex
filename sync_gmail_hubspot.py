#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors the Gmail inbox and syncs sender contacts to HubSpot CRM,
avoiding duplicates and updating existing records with missing data.

Usage:
    python sync_gmail_hubspot.py

Required env vars:
    HUBSPOT_ACCESS_TOKEN  - HubSpot private app token

Required files (same directory):
    credentials.json      - Google OAuth2 client credentials
    token.pickle          - auto-generated on first run (OAuth flow)

Output per email processed:
    status     Creato / Aggiornato / Ignorato
    email      sender email address
    contact_id HubSpot contact ID
"""

import json
import os
import pickle
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

# ── Third-party (see requirements.txt) ───────────────────────────────────────
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.pickle"
STATE_FILE = BASE_DIR / ".sync_state.json"

# ── Gmail ─────────────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_QUERY = "in:inbox -from:me"
MAX_EMAILS = 200

# ── Sender filters ────────────────────────────────────────────────────────────
AUTOMATED_LOCAL_PATTERNS = re.compile(
    r"^(no.?reply|noreply|do.?not.?reply|notify|notification|notifica"
    r"|mailer|nobody|automat|conferma|premium|support.ticket"
    r"|bounce|daemon|postmaster|devnull|info-noreply|alert)@",
    re.IGNORECASE,
)

AUTOMATED_DOMAINS = {
    "google.com", "youtube.com", "amazon.com", "amazon.it",
    "shop.tiktok.com", "revolut.com", "email.patreon.com",
    "skool.com", "manychat.com", "academia-mail.com",
    "engage.canva.com", "notification.circle.so",
    "feedspot.com", "serpapi.com", "mail02.usercentrics.eu",
    "moneya.es", "semalt.org",
}

# ── HubSpot ───────────────────────────────────────────────────────────────────
LEAD_SOURCE = "GMAIL"
CONTACT_TAG = "Inbound Gmail"
FETCH_PROPERTIES = ["email", "firstname", "lastname", "company", "hs_lead_source"]


# ─────────────────────────────────────────────────────────────────────────────
# State persistence (tracks processed message IDs to avoid reprocessing)
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_message_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Gmail
# ─────────────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as fh:
            creds = pickle.load(fh)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as fh:
            pickle.dump(creds, fh)

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, processed_ids: set) -> list[dict]:
    """Return new inbox messages (not yet processed) as dicts with sender info."""
    result = service.users().messages().list(
        userId="me", q=GMAIL_QUERY, maxResults=MAX_EMAILS
    ).execute()

    messages = []
    for ref in result.get("messages", []):
        if ref["id"] in processed_ids:
            continue
        msg = service.users().messages().get(
            userId="me",
            id=ref["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "")
        if from_header:
            messages.append({
                "id": ref["id"],
                "from": from_header,
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            })

    return messages


# ─────────────────────────────────────────────────────────────────────────────
# Sender parsing & filtering
# ─────────────────────────────────────────────────────────────────────────────

def is_automated(email: str) -> bool:
    email = email.lower()
    domain = email.split("@")[-1] if "@" in email else ""
    if domain in AUTOMATED_DOMAINS:
        return True
    return bool(AUTOMATED_LOCAL_PATTERNS.match(email))


def parse_sender(raw_from: str) -> dict:
    """
    Parse a From header into structured contact fields.

    'Mirco Gasparotto <info@mircogasparotto.com>' →
        {email, firstname, lastname, company, domain}
    """
    display_name, email = parseaddr(raw_from)
    email = email.lower().strip()
    domain = email.split("@")[-1] if "@" in email else ""

    # Name
    firstname, lastname = "", ""
    name = display_name.strip()
    if name:
        parts = name.split(None, 1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""
    else:
        # Fall back to local part of email
        firstname = email.split("@")[0].capitalize()

    # Company: strip TLD and capitalise each word
    company = (
        re.sub(r"\.(com|it|eu|net|org|io|ai|sh|us|co)$", "", domain, flags=re.I)
        .replace(".", " ")
        .replace("-", " ")
        .title()
        if domain
        else ""
    )

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HubSpot
# ─────────────────────────────────────────────────────────────────────────────

def hubspot_find(hs: hubspot.Client, email: str):
    """Return existing HubSpot contact or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
        ],
        properties=FETCH_PROPERTIES,
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0] if resp.results else None


def hubspot_create(hs: hubspot.Client, sender: dict) -> tuple[str, str]:
    """Create a new contact. Returns (status, id)."""
    props = {
        "email": sender["email"],
        "firstname": sender["firstname"],
        "company": sender["company"],
        "hs_lead_source": LEAD_SOURCE,
    }
    if sender["lastname"]:
        props["lastname"] = sender["lastname"]

    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return "Creato", result.id
    except ApiException as exc:
        if exc.status == 409:
            # Race condition — another process created it between our search and create
            existing = hubspot_find(hs, sender["email"])
            return "Ignorato", existing.id if existing else "?"
        raise


def hubspot_update(hs: hubspot.Client, contact, sender: dict) -> tuple[str, str]:
    """Fill in missing fields on an existing contact. Returns (status, id)."""
    p = contact.properties or {}
    updates = {}

    if not p.get("firstname") and sender["firstname"]:
        updates["firstname"] = sender["firstname"]
    if not p.get("lastname") and sender["lastname"]:
        updates["lastname"] = sender["lastname"]
    if not p.get("company") and sender["company"]:
        updates["company"] = sender["company"]
    if not p.get("hs_lead_source"):
        updates["hs_lead_source"] = LEAD_SOURCE

    if not updates:
        return "Ignorato", contact.id

    hs.crm.contacts.basic_api.update(
        contact_id=contact.id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return "Aggiornato", contact.id


# ─────────────────────────────────────────────────────────────────────────────
# Main sync loop
# ─────────────────────────────────────────────────────────────────────────────

def sync(gmail_service, hs: hubspot.Client, state: dict) -> list[dict]:
    processed_ids = set(state.get("processed_message_ids", []))
    new_messages = fetch_inbox_messages(gmail_service, processed_ids)

    results = []
    seen_in_batch: set[str] = set()

    for msg in new_messages:
        processed_ids.add(msg["id"])
        sender = parse_sender(msg["from"])
        email = sender["email"]

        if is_automated(email):
            continue  # silently skip

        if email in seen_in_batch:
            continue  # deduplicate within batch
        seen_in_batch.add(email)

        existing = hubspot_find(hs, email)
        if existing:
            status, contact_id = hubspot_update(hs, existing, sender)
        else:
            status, contact_id = hubspot_create(hs, sender)

        results.append(
            {
                "status": status,
                "email": email,
                "contact_id": contact_id,
                "subject": msg["subject"],
            }
        )

    # Persist — keep only the last 2000 IDs to bound file size
    state["processed_message_ids"] = list(processed_ids)[-2000:]
    save_state(state)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    hubspot_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN environment variable is not set.")

    state = load_state()
    gmail_svc = get_gmail_service()
    hs_client = hubspot.Client.create(access_token=hubspot_token)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{ts}] Gmail → HubSpot sync started")

    results = sync(gmail_svc, hs_client, state)

    if results:
        print(f"\n{'STATO':<12} {'EMAIL':<48} HUBSPOT ID")
        print("─" * 75)
        for r in results:
            print(f"{r['status']:<12} {r['email']:<48} {r['contact_id']}")
        created = sum(1 for r in results if r["status"] == "Creato")
        updated = sum(1 for r in results if r["status"] == "Aggiornato")
        ignored = sum(1 for r in results if r["status"] == "Ignorato")
        print(f"\nProcessati: {len(results)}  |  Creati: {created}  |  Aggiornati: {updated}  |  Ignorati: {ignored}")
    else:
        print("Nessun nuovo contatto da sincronizzare.")


if __name__ == "__main__":
    main()
