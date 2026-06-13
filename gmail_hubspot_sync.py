#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for incoming emails, extracts sender data,
and creates/updates HubSpot contacts without duplicates.

Required env vars:
  GMAIL_CREDENTIALS_FILE   Path to OAuth2 credentials JSON (from Google Cloud Console)
  GMAIL_TOKEN_FILE         Path to store/read the access token (default: token.json)
  HUBSPOT_ACCESS_TOKEN     HubSpot private app access token
  GMAIL_USER_EMAIL         Gmail address to monitor (default: me)
  LOOKBACK_HOURS           Hours to look back on first run (default: 24)
  STATE_FILE               JSON file that persists last-run timestamp (default: .sync_state.json)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"

# Domains / patterns to always skip (automated senders)
SKIP_DOMAINS = {
    "facebookmail.com", "notifications.google.com", "accounts.google.com",
    "mailer-daemon.googlemail.com", "bounce.em.constant", "noreply",
    "no-reply", "mailvox.it",
}

SKIP_LOCAL_PARTS = {"noreply", "no-reply", "mailer-daemon", "postmaster", "bounce"}


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    creds_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")

    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, since_epoch_ms: int, user: str = "me"):
    """Yield message dicts (id, threadId) for inbox messages newer than since_epoch_ms."""
    query = f"in:inbox after:{int(since_epoch_ms / 1000)} -from:me -is:draft"
    page_token = None
    while True:
        resp = service.users().messages().list(
            userId=user, q=query, maxResults=500,
            pageToken=page_token
        ).execute()
        for msg in resp.get("messages", []):
            yield msg
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def get_message_headers(service, msg_id: str, user: str = "me") -> dict:
    msg = service.users().messages().get(
        userId=user, id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"]
    ).execute()
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers


# ---------------------------------------------------------------------------
# Sender extraction
# ---------------------------------------------------------------------------

def extract_company_from_domain(domain: str) -> str:
    parts = domain.split(".")
    # Strip common TLDs and 'www'
    tld_parts = {"com", "it", "org", "net", "gov", "edu", "io", "co", "uk", "fr", "de", "ch"}
    filtered = [p for p in parts if p not in tld_parts and p != "www"]
    if filtered:
        return filtered[-1].replace("-", " ").title()
    return domain.title()


def parse_sender(from_header: str) -> dict | None:
    """
    Parse a 'From' header and return a dict with email, first_name,
    last_name, company. Returns None if the sender should be skipped.
    """
    display_name, email = parseaddr(from_header)
    if not email or "@" not in email:
        return None

    email = email.strip().lower()
    local, _, domain = email.partition("@")

    # Skip automated senders
    if any(skip in domain for skip in SKIP_DOMAINS):
        return None
    if any(local.startswith(p) for p in SKIP_LOCAL_PARTS):
        return None
    if "+" in local and any(skip in local for skip in SKIP_LOCAL_PARTS):
        return None

    company = extract_company_from_domain(domain)

    # Try to split display name into first/last
    first_name = last_name = ""
    clean_name = display_name.strip().strip('"')
    if clean_name and not re.match(r"^[\w.+-]+@", clean_name):
        parts = clean_name.split(None, 1)
        first_name = parts[0].title() if parts else ""
        last_name = parts[1].title() if len(parts) > 1 else ""

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def hs_headers() -> dict:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN is not set")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def hs_find_contact_by_email(email: str) -> dict | None:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
        "limit": 1,
    }
    resp = requests.post(url, headers=hs_headers(), json=body, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: dict) -> dict:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    props = {
        "email": sender["email"],
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    resp = requests.post(url, headers=hs_headers(), json={"properties": props}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str | int, missing_props: dict) -> dict:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=hs_headers(), json={"properties": missing_props}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def sync_sender(sender: dict) -> tuple[str, str | int]:
    """
    Check HubSpot for the sender and create/update accordingly.
    Returns (status, contact_id) where status ∈ {"CREATED", "UPDATED", "IGNORED"}.
    """
    existing = hs_find_contact_by_email(sender["email"])

    if existing is None:
        new_contact = hs_create_contact(sender)
        return "CREATED", new_contact["id"]

    # Contact exists — collect missing fields
    props = existing.get("properties", {})
    updates = {}

    if not props.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not props.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not props.get("company") and sender["company"]:
        updates["company"] = sender["company"]
    if not props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    contact_id = existing["id"]
    if updates:
        hs_update_contact(contact_id, updates)
        return "UPDATED", contact_id

    return "IGNORED", contact_id


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state(state_file: str) -> dict:
    if Path(state_file).exists():
        with open(state_file) as fh:
            return json.load(fh)
    lookback_h = int(os.getenv("LOOKBACK_HOURS", "24"))
    epoch_ms = int((time.time() - lookback_h * 3600) * 1000)
    return {"last_run_epoch_ms": epoch_ms}


def save_state(state_file: str, state: dict):
    with open(state_file, "w") as fh:
        json.dump(state, fh, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    state_file = os.getenv("STATE_FILE", ".sync_state.json")
    gmail_user = os.getenv("GMAIL_USER_EMAIL", "me")

    state = load_state(state_file)
    since_ms = state["last_run_epoch_ms"]
    run_start_ms = int(time.time() * 1000)

    log.info("Starting sync — looking for emails since %s",
             datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).isoformat())

    service = get_gmail_service()

    seen_emails: set[str] = set()
    results: list[dict] = []

    for msg_meta in fetch_inbox_messages(service, since_ms, gmail_user):
        headers = get_message_headers(service, msg_meta["id"], gmail_user)
        from_header = headers.get("from", "")
        if not from_header:
            continue

        sender = parse_sender(from_header)
        if sender is None or sender["email"] in seen_emails:
            continue
        seen_emails.add(sender["email"])

        try:
            status, contact_id = sync_sender(sender)
        except requests.HTTPError as exc:
            log.error("HubSpot error for %s: %s", sender["email"], exc)
            status, contact_id = "ERROR", "N/A"

        record = {
            "status": status,
            "email": sender["email"],
            "contact_id": contact_id,
            "subject": headers.get("subject", ""),
        }
        results.append(record)
        log.info("[%s] %s → HubSpot ID %s", status, sender["email"], contact_id)

    # Summary
    created = sum(1 for r in results if r["status"] == "CREATED")
    updated = sum(1 for r in results if r["status"] == "UPDATED")
    ignored = sum(1 for r in results if r["status"] == "IGNORED")
    errors = sum(1 for r in results if r["status"] == "ERROR")

    log.info("Done — processed %d senders: %d created, %d updated, %d ignored, %d errors",
             len(results), created, updated, ignored, errors)

    # Print table
    print("\n| Status   | Email                                       | HubSpot ID        |")
    print("|----------|---------------------------------------------|-------------------|")
    for r in results:
        print(f"| {r['status']:<8} | {r['email']:<43} | {str(r['contact_id']):<17} |")

    # Save state
    state["last_run_epoch_ms"] = run_start_ms
    state["last_run_at"] = datetime.fromtimestamp(run_start_ms / 1000, tz=timezone.utc).isoformat()
    state["last_run_summary"] = {"created": created, "updated": updated,
                                 "ignored": ignored, "errors": errors}
    save_state(state_file, state)

    return errors == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
