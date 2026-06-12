#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors inbox emails and upserts sender contacts into HubSpot.
"""

import json
import os
import re
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min default

# Domains to skip (no-reply, newsletters, system senders)
SKIP_DOMAINS = {
    "noreply.com", "no-reply.com", "mailer.com", "bounce.com",
    "amazonses.com", "sendgrid.net", "mailchimp.com", "voxmail.it",
    "app.mailvox.it", "mcsv.net", "mandrillapp.com",
}

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── State management ───────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": [], "last_run": None}


def save_state(state: dict):
    state_copy = state.copy()
    # Keep only the last 5000 IDs to avoid unbounded growth
    state_copy["processed_ids"] = state["processed_ids"][-5000:]
    STATE_FILE.write_text(json.dumps(state_copy, indent=2))


# ── Gmail client ───────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, processed_ids: list) -> list:
    """Return unread inbox messages not yet processed."""
    results = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], q="in:inbox -from:me", maxResults=50)
        .execute()
    )
    messages = results.get("messages", [])
    new_msgs = [m for m in messages if m["id"] not in processed_ids]
    return new_msgs


def get_message_headers(service, msg_id: str) -> dict:
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata",
             metadataHeaders=["From", "Subject", "Date"])
        .execute()
    )
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    return headers


# ── Contact extraction ─────────────────────────────────────────────────────────

_FROM_RE = re.compile(r'^"?([^"<]+?)"?\s*<([^>]+)>$|^([^\s@]+@[^\s@]+)$')


def parse_sender(from_header: str) -> tuple[str | None, str | None, str | None]:
    """
    Parse 'From' header into (firstname, lastname, email).
    Handles both 'Name <email>' and bare 'email' formats.
    """
    from_header = from_header.strip()
    m = _FROM_RE.match(from_header)
    if not m:
        return None, None, None

    if m.group(3):  # bare email
        email = m.group(3).lower()
        firstname, lastname = _name_from_email_local(email.split("@")[0])
        return firstname, lastname, email

    name_raw = m.group(1).strip()
    email = m.group(2).strip().lower()
    parts = name_raw.split(maxsplit=1)
    firstname = parts[0].title() if parts else None
    lastname = parts[1].title() if len(parts) > 1 else None
    return firstname, lastname, email


def _name_from_email_local(local: str) -> tuple[str | None, str | None]:
    """Try to infer first/last name from the local part of an email address."""
    # Remove trailing digits (e.g. filippo.lippi.1987 → filippo.lippi)
    local = re.sub(r'\d+', '', local).strip(".")
    parts = re.split(r'[._\-]', local)
    parts = [p.title() for p in parts if p]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    if len(parts) == 1:
        return parts[0], None
    return None, None


def company_from_domain(domain: str) -> str | None:
    """Derive a readable company name from the email domain."""
    # Strip common TLDs and subdomains
    skip = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "icloud.com", "libero.it", "virgilio.it", "tin.it"}
    if domain.lower() in skip:
        return None

    # Remove TLD(s): e.g. comune.sanseverinomarche.mc.it → sanseverinomarche
    parts = domain.rstrip(".").split(".")
    # Take the second-level domain as company hint
    core = parts[-2] if len(parts) >= 2 else parts[0]
    core = re.sub(r'[\-_]', ' ', core).title()
    return core or None


def should_skip(email: str) -> bool:
    """Return True for system/newsletter addresses that shouldn't be synced."""
    if not email or "@" not in email:
        return True
    domain = email.split("@", 1)[1].lower()
    if domain in SKIP_DOMAINS:
        return True
    local = email.split("@")[0].lower()
    if local in {"noreply", "no-reply", "donotreply", "bounce", "mailer-daemon",
                 "postmaster", "daemon", "admin", "newsletter"}:
        return True
    return False


# ── HubSpot client ─────────────────────────────────────────────────────────────

def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(hs: hubspot.Client, email: str) -> dict | None:
    """Return existing HubSpot contact dict or None."""
    fil = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[fil])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company",
                    "hs_analytics_source", "message"],
        limit=1,
    )
    try:
        resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
        if resp.total > 0:
            return resp.results[0]
    except ApiException as e:
        log.error("HubSpot search error for %s: %s", email, e)
    return None


def create_contact(hs: hubspot.Client, props: dict) -> str | None:
    """Create a new HubSpot contact. Returns contact ID or None on error."""
    obj = SimplePublicObjectInputForCreate(properties=props)
    try:
        resp = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        return resp.id
    except ApiException as e:
        log.error("HubSpot create error: %s", e)
    return None


def update_contact(hs: hubspot.Client, contact_id: str, props: dict) -> bool:
    """Patch a HubSpot contact with new property values."""
    from hubspot.crm.contacts import SimplePublicObjectInput
    obj = SimplePublicObjectInput(properties=props)
    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id, simple_public_object_input=obj
        )
        return True
    except ApiException as e:
        log.error("HubSpot update error for %s: %s", contact_id, e)
    return False


# ── Sync logic ─────────────────────────────────────────────────────────────────

def build_contact_props(firstname, lastname, email, company) -> dict:
    props = {
        "email": email,
        "hs_lead_status": "NEW",
        "hs_analytics_source": "OTHER_CAMPAIGNS",
        "message": "Fonte: Gmail Inbound | Tag: Inbound Gmail",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def sync_contact(hs: hubspot.Client, firstname, lastname, email, company) -> dict:
    """
    Upsert contact in HubSpot.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "id": ...}
    """
    existing = find_contact_by_email(hs, email)

    if existing:
        ep = existing.properties
        updates = {}

        # Fill in any missing fields
        if firstname and not ep.get("firstname"):
            updates["firstname"] = firstname
        if lastname and not ep.get("lastname"):
            updates["lastname"] = lastname
        if company and not ep.get("company"):
            updates["company"] = company
        # Always ensure source note is present
        if not ep.get("message") or "Inbound Gmail" not in ep.get("message", ""):
            existing_msg = ep.get("message", "") or ""
            updates["message"] = (
                existing_msg + " | Fonte: Gmail Inbound | Tag: Inbound Gmail"
            ).lstrip(" | ")

        if updates:
            ok = update_contact(hs, existing.id, updates)
            status = "Aggiornato" if ok else "Ignorato"
        else:
            status = "Ignorato"

        return {"status": status, "email": email, "id": existing.id}

    props = build_contact_props(firstname, lastname, email, company)
    new_id = create_contact(hs, props)
    if new_id:
        return {"status": "Creato", "email": email, "id": new_id}
    return {"status": "Ignorato", "email": email, "id": None}


# ── Main loop ──────────────────────────────────────────────────────────────────

def run_once(gmail, hs, state: dict) -> list[dict]:
    processed_ids: list = state["processed_ids"]
    results = []

    new_messages = fetch_new_messages(gmail, processed_ids)
    log.info("Found %d new message(s) to process.", len(new_messages))

    for msg in new_messages:
        msg_id = msg["id"]
        try:
            headers = get_message_headers(gmail, msg_id)
        except Exception as e:
            log.warning("Could not fetch headers for %s: %s", msg_id, e)
            processed_ids.append(msg_id)
            continue

        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")

        firstname, lastname, email = parse_sender(from_header)

        if not email or should_skip(email):
            log.debug("Skipping %s (filtered)", email or from_header)
            processed_ids.append(msg_id)
            continue

        domain = email.split("@", 1)[1]
        company = company_from_domain(domain)

        log.info("Processing <%s> — subject: %s", email, subject[:60])
        result = sync_contact(hs, firstname, lastname, email, company)
        result["subject"] = subject
        results.append(result)

        log.info(
            "  %-10s | email: %-45s | ID: %s",
            result["status"], result["email"], result["id"] or "—",
        )
        processed_ids.append(msg_id)
        time.sleep(0.2)  # respect HubSpot rate limits

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    return results


def main():
    log.info("Starting Gmail → HubSpot sync (interval: %ds)", POLL_INTERVAL_SECONDS)

    gmail = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()

    while True:
        try:
            results = run_once(gmail, hs, state)
            save_state(state)

            created = sum(1 for r in results if r["status"] == "Creato")
            updated = sum(1 for r in results if r["status"] == "Aggiornato")
            skipped = sum(1 for r in results if r["status"] == "Ignorato")
            log.info(
                "Sync complete — Creati: %d | Aggiornati: %d | Ignorati: %d",
                created, updated, skipped,
            )
        except Exception as e:
            log.error("Unexpected error during sync: %s", e, exc_info=True)

        log.info("Next check in %d seconds…", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
