#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails, extracts sender info,
and creates/updates contacts in HubSpot.
"""

import os
import re
import json
import time
import base64
import logging
import argparse
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput, ApiException
from hubspot.crm.contacts.api import BasicApi as ContactsBasicApi
from hubspot.crm.contacts.api import SearchApi as ContactsSearchApi

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Gmail OAuth scopes (read-only is sufficient)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains to skip (internal / transactional senders)
SKIP_DOMAINS = {
    "noreply", "no-reply", "mailer-daemon", "postmaster",
    "bounce", "notifications", "accounts.google.com",
    "gmail.com", "googlemail.com",
}

STATE_FILE = Path(".sync_state.json")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    token_path = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
    creds_path = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Google OAuth credentials not found at {creds_path}. "
                    "Download credentials.json from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Return (name, email, domain) from a From: header."""
    name, email_addr = parseaddr(from_header)
    email_addr = email_addr.strip().lower()
    domain = email_addr.split("@")[-1] if "@" in email_addr else ""
    # Clean encoded name
    name = name.strip().strip('"')
    return name, email_addr, domain


def split_name(full_name: str) -> tuple[str, str]:
    """Heuristically split 'First Last' → (first, last)."""
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return full_name, ""


def company_from_domain(domain: str) -> str:
    """Turn 'acme.com' → 'Acme'."""
    if not domain:
        return ""
    name = domain.split(".")[0]
    return name.capitalize()


def fetch_new_messages(service, state: dict, max_results: int = 50) -> list[dict]:
    """
    Fetch messages received after the last run.
    Falls back to newest 50 inbox messages on first run.
    """
    query = "in:inbox -from:me"
    last_id = state.get("last_history_id")

    if last_id:
        # Use history API for incremental fetch
        try:
            history = (
                service.users()
                .history()
                .list(userId="me", startHistoryId=last_id, historyTypes=["messageAdded"])
                .execute()
            )
            message_ids = []
            for record in history.get("history", []):
                for m in record.get("messagesAdded", []):
                    message_ids.append(m["message"]["id"])
            if not message_ids:
                log.info("No new messages since last run.")
                return []
            # Update history id
            state["last_history_id"] = history.get("historyId", last_id)
            save_state(state)
            messages = []
            for mid in message_ids:
                msg = service.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Date", "Subject"]
                ).execute()
                messages.append(msg)
            return messages
        except Exception as exc:
            log.warning("History API failed (%s), falling back to list.", exc)

    # First run: fetch latest inbox messages
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    raw_messages = result.get("messages", [])
    messages = []
    for m in raw_messages:
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Date", "Subject"]
        ).execute()
        messages.append(msg)

    # Save the historyId from the last message for incremental updates
    if messages:
        state["last_history_id"] = messages[0].get("historyId")
        save_state(state)
    return messages


def extract_sender(message: dict) -> dict | None:
    """Return sender dict or None if the message should be skipped."""
    headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
    from_header = headers.get("From", "")
    date_str = headers.get("Date", "")
    subject = headers.get("Subject", "(no subject)")

    if not from_header:
        return None

    name, email_addr, domain = parse_sender(from_header)

    if not email_addr or "@" not in email_addr:
        return None

    # Skip automated / internal senders
    local = email_addr.split("@")[0]
    if domain in SKIP_DOMAINS or any(kw in local for kw in ("noreply", "no-reply", "bounce")):
        log.debug("Skipping automated sender: %s", email_addr)
        return None

    first, last = split_name(name)
    company = company_from_domain(domain)

    return {
        "message_id": message["id"],
        "email": email_addr,
        "first_name": first,
        "last_name": last,
        "company": company,
        "domain": domain,
        "subject": subject,
        "date": date_str,
    }


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def get_hubspot_client():
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN not set in environment.")
    return hubspot.Client.create(access_token=token)


def find_contact_by_email(client, email: str) -> dict | None:
    """Return existing HubSpot contact or None."""
    from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup

    search_filter = Filter(property_name="email", operator="EQ", value=email)
    filter_group = FilterGroup(filters=[search_filter])
    request = PublicObjectSearchRequest(
        filter_groups=[filter_group],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=request)
        if resp.total > 0:
            return resp.results[0]
    except ApiException as exc:
        log.error("HubSpot search error: %s", exc)
    return None


def create_contact(client, sender: dict) -> dict | None:
    """Create a new HubSpot contact."""
    props = {
        "email": sender["email"],
        "firstname": sender["first_name"],
        "lastname": sender["last_name"],
        "company": sender["company"],
        "hs_lead_source": "Gmail",
    }
    # Remove empty values
    props = {k: v for k, v in props.items() if v}

    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=hubspot.crm.contacts.SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", sender["email"], exc)
    return None


def update_contact(client, contact_id: str, sender: dict, existing) -> dict | None:
    """Update only missing/empty fields on an existing HubSpot contact."""
    existing_props = existing.properties or {}
    updates = {}

    field_map = {
        "firstname": sender["first_name"],
        "lastname": sender["last_name"],
        "company": sender["company"],
    }
    for hs_field, new_value in field_map.items():
        if new_value and not existing_props.get(hs_field):
            updates[hs_field] = new_value

    if not updates:
        return existing  # Nothing to update

    try:
        result = client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return result
    except ApiException as exc:
        log.error("HubSpot update error for %s: %s", sender["email"], exc)
    return None


def add_timeline_note(client, contact_id: str, sender: dict) -> None:
    """Log an 'email received' engagement on the contact timeline."""
    try:
        note_body = (
            f"📧 Email received via Gmail\n"
            f"From: {sender['first_name']} {sender['last_name']} <{sender['email']}>\n"
            f"Subject: {sender['subject']}\n"
            f"Date: {sender['date']}"
        )
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=hubspot.crm.objects.notes.SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(time.time() * 1000)),
                },
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                    }
                ],
            )
        )
    except Exception as exc:
        log.debug("Could not add timeline note: %s", exc)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_sender(client, sender: dict, state: dict, add_notes: bool) -> dict:
    """
    Decide whether to create, update, or ignore the contact.
    Returns a result row dict.
    """
    email = sender["email"]
    msg_id = sender["message_id"]

    # Skip already processed messages
    if msg_id in state.get("processed_ids", []):
        return {"status": "Ignorato", "email": email, "hubspot_id": None, "reason": "già processato"}

    existing = find_contact_by_email(client, email)

    if existing:
        contact_id = existing.id
        updated = update_contact(client, contact_id, sender, existing)
        action = "Aggiornato" if updated and updated != existing else "Ignorato"
        if add_notes:
            add_timeline_note(client, contact_id, sender)
        result = {"status": action, "email": email, "hubspot_id": contact_id}
    else:
        created = create_contact(client, sender)
        if created:
            contact_id = created.id
            if add_notes:
                add_timeline_note(client, contact_id, sender)
            result = {"status": "Creato", "email": email, "hubspot_id": contact_id}
        else:
            result = {"status": "Errore", "email": email, "hubspot_id": None}

    # Mark message as processed
    processed = state.setdefault("processed_ids", [])
    processed.append(msg_id)
    # Keep only last 5000 to avoid unbounded growth
    state["processed_ids"] = processed[-5000:]
    save_state(state)

    return result


def run_once(gmail_service, hs_client, state: dict, add_notes: bool, max_results: int) -> list[dict]:
    messages = fetch_new_messages(gmail_service, state, max_results=max_results)
    log.info("Fetched %d message(s) to process.", len(messages))

    results = []
    for msg in messages:
        sender = extract_sender(msg)
        if not sender:
            continue
        result = process_sender(hs_client, sender, state, add_notes)
        results.append(result)
        log.info(
            "[%s] %s  →  HubSpot ID: %s",
            result["status"].upper(),
            result["email"],
            result.get("hubspot_id") or "—",
        )

    return results


def print_summary(results: list[dict]) -> None:
    if not results:
        print("\nNessun nuovo contatto da processare.")
        return
    print("\n" + "=" * 60)
    print(f"{'Stato':<12} {'Email':<35} {'HubSpot ID'}")
    print("-" * 60)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<35} {r.get('hubspot_id') or '—'}")
    print("=" * 60)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for status, n in counts.items():
        print(f"  {status}: {n}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--loop", action="store_true", help="Run continuously (polling)")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval in seconds (default: 300)")
    parser.add_argument("--max-emails", type=int, default=50, help="Max emails to fetch per run (default: 50)")
    parser.add_argument("--notes", action="store_true", default=True,
                        help="Add timeline note to HubSpot contact (default: on)")
    parser.add_argument("--no-notes", dest="notes", action="store_false")
    return parser.parse_args()


def main():
    args = parse_args()
    log.info("Initialising Gmail service …")
    gmail_service = get_gmail_service()
    log.info("Initialising HubSpot client …")
    hs_client = get_hubspot_client()

    if args.loop:
        log.info("Starting continuous sync (interval: %ds). Press Ctrl+C to stop.", args.interval)
        while True:
            state = load_state()
            try:
                results = run_once(gmail_service, hs_client, state, args.notes, args.max_emails)
                print_summary(results)
            except Exception as exc:
                log.error("Sync cycle failed: %s", exc, exc_info=True)
            log.info("Next run in %ds …", args.interval)
            time.sleep(args.interval)
    else:
        state = load_state()
        results = run_once(gmail_service, hs_client, state, args.notes, args.max_emails)
        print_summary(results)


if __name__ == "__main__":
    main()
