#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts senders, creates/updates HubSpot contacts.

Usage:
    python gmail_hubspot_sync.py [--once] [--interval 300]

Required env vars:
    HUBSPOT_ACCESS_TOKEN   HubSpot private app token
    GMAIL_CREDENTIALS_FILE Path to Google OAuth2 credentials.json (default: credentials.json)
    GMAIL_TOKEN_FILE       Path to token cache (default: token.json)

Optional env vars:
    SYNC_INTERVAL_SECONDS  Poll interval in seconds (default: 300)
    STATE_FILE             Path to last-seen message state (default: sync_state.json)
    IGNORED_DOMAINS        Comma-separated domains to skip (e.g. facebookmail.com,noreply.com)
"""

import json
import os
import re
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

# ── third-party ──────────────────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    raise SystemExit("Install: pip install google-api-python-client google-auth-oauthlib")

try:
    import hubspot
    from hubspot.crm.contacts import ApiException, SimplePublicObjectInputForCreate
    from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
except ImportError:
    raise SystemExit("Install: pip install hubspot-api-client")

# ── config ────────────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

IGNORED_DOMAINS = {
    d.strip()
    for d in os.getenv("IGNORED_DOMAINS", "facebookmail.com,noreply.com,mailer-daemon.com").split(",")
    if d.strip()
}

PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it", "tin.it", "tiscali.it"}

FORWARDED_RE = re.compile(
    r'Da\s+"([^"]+)"\s+([\w.+%-]+@[\w.-]+\.[A-Za-z]{2,})',
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hubspot_sync")


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def build_gmail_service(credentials_file: str, token_file: str):
    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def parse_sender(from_header: str) -> tuple[str | None, str | None, str | None]:
    """Return (firstname, lastname, email) from a RFC 5322 From header."""
    email_match = re.search(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", from_header)
    if not email_match:
        return None, None, None
    email = email_match.group(0).lower()
    name_match = re.match(r'^"?([^"<]+?)"?\s*<', from_header)
    full_name = name_match.group(1).strip() if name_match else ""
    parts = full_name.split(None, 1)
    firstname = parts[0].title() if parts else None
    lastname = parts[1].title() if len(parts) > 1 else None
    return firstname, lastname, email


def company_from_domain(domain: str) -> str | None:
    if domain in PERSONAL_DOMAINS:
        return None
    parts = domain.split(".")
    # drop generic TLDs; take the most meaningful segment
    meaningful = [p for p in parts if p not in ("www", "mail", "smtp", "it", "com", "org", "net", "eu", "gov", "edu")]
    if not meaningful:
        return None
    return meaningful[0].replace("-", " ").title()


def fetch_new_messages(service, state: dict) -> list[dict]:
    """Return inbox messages newer than the last seen history ID."""
    query = "in:inbox -from:me"
    messages = []
    page_token = None
    while True:
        params = {"userId": "me", "q": query, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = service.users().messages().list(**params).execute()
        batch = resp.get("messages", [])
        if not batch:
            break

        for msg_stub in batch:
            mid = msg_stub["id"]
            if mid == state.get("last_message_id"):
                return messages  # reached already-processed boundary
            msg = service.users().messages().get(userId="me", id=mid, format="metadata",
                                                  metadataHeaders=["From", "Subject", "Date"]).execute()
            messages.append(msg)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return messages


def extract_contacts_from_message(msg: dict) -> list[tuple[str | None, str | None, str]]:
    """
    Returns a list of (firstname, lastname, email) tuples.
    Handles both direct senders and forwarded-email patterns (Fw:).
    """
    contacts = []
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    from_header = headers.get("From", "")
    subject = headers.get("Subject", "")

    firstname, lastname, email = parse_sender(from_header)
    if email:
        contacts.append((firstname, lastname, email))

    # For forwarded messages, also extract the original sender from snippet
    if re.match(r"^(Fw|Fwd):", subject, re.IGNORECASE):
        snippet = msg.get("snippet", "")
        for match in FORWARDED_RE.finditer(snippet):
            full_name = match.group(1).strip()
            orig_email = match.group(2).lower()
            parts = full_name.split(None, 1)
            fn = parts[0].title() if parts else None
            ln = parts[1].title() if len(parts) > 1 else None
            contacts.append((fn, ln, orig_email))

    return contacts


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def build_hubspot_client(token: str):
    return hubspot.Client.create(access_token=token)


def search_contact_by_email(hs_client, email: str) -> dict | None:
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    resp = hs_client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0].to_dict() if resp.results else None


def upsert_contact(hs_client, firstname, lastname, email, company) -> tuple[str, str]:
    """
    Returns (status, contact_id) where status is 'created', 'updated', or 'ignored'.
    """
    existing = search_contact_by_email(hs_client, email)
    props = {"email": email, "hs_lead_source": "OTHER"}  # map Gmail → OTHER (closest standard value)

    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    if existing:
        contact_id = existing["id"]
        ex_props = existing.get("properties", {})
        update_props = {}
        for k, v in props.items():
            if k == "email":
                continue
            if not ex_props.get(k) and v:
                update_props[k] = v
        if update_props:
            hs_client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(properties=update_props),
            )
            return "updated", contact_id
        return "ignored", contact_id
    else:
        resp = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props),
        )
        return "created", resp.id


# ── state management ──────────────────────────────────────────────────────────

def load_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict):
    Path(path).write_text(json.dumps(state, indent=2))


# ── main sync loop ────────────────────────────────────────────────────────────

def run_sync(gmail_service, hs_client, state: dict, state_file: str):
    log.info("Starting sync pass…")
    messages = fetch_new_messages(gmail_service, state)
    if not messages:
        log.info("No new messages.")
        return

    log.info("Found %d new message(s).", len(messages))

    results = []
    seen_emails: set[str] = set()

    for msg in messages:
        for firstname, lastname, email in extract_contacts_from_message(msg):
            domain = email.split("@")[-1]
            if domain in IGNORED_DOMAINS:
                log.debug("Skipping ignored domain: %s", email)
                continue
            if email in seen_emails:
                continue
            seen_emails.add(email)

            company = company_from_domain(domain)
            try:
                status, contact_id = upsert_contact(hs_client, firstname, lastname, email, company)
            except ApiException as exc:
                log.error("HubSpot API error for %s: %s", email, exc)
                status, contact_id = "error", "—"

            icon = {"created": "✅", "updated": "🔄", "ignored": "⏭️", "error": "❌"}.get(status, "?")
            log.info("%s %-10s  %-40s  ID: %s", icon, status.upper(), email, contact_id)
            results.append({"status": status, "email": email, "hubspot_id": str(contact_id)})

    if messages:
        state["last_message_id"] = messages[0]["id"]
        save_state(state_file, state)

    created = sum(1 for r in results if r["status"] == "created")
    updated = sum(1 for r in results if r["status"] == "updated")
    ignored = sum(1 for r in results if r["status"] == "ignored")
    log.info("Done — created: %d  updated: %d  ignored: %d", created, updated, ignored)
    return results


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run one sync pass and exit")
    parser.add_argument("--interval", type=int, default=int(os.getenv("SYNC_INTERVAL_SECONDS", 300)),
                        help="Poll interval in seconds (default: 300)")
    args = parser.parse_args()

    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    state_file = os.getenv("STATE_FILE", "sync_state.json")

    if not hubspot_token:
        raise SystemExit("Set the HUBSPOT_ACCESS_TOKEN environment variable.")

    gmail_service = build_gmail_service(credentials_file, token_file)
    hs_client = build_hubspot_client(hubspot_token)
    state = load_state(state_file)

    if args.once:
        run_sync(gmail_service, hs_client, state, state_file)
        return

    log.info("Continuous mode — polling every %ds. Ctrl+C to stop.", args.interval)
    while True:
        try:
            run_sync(gmail_service, hs_client, state, state_file)
        except Exception as exc:
            log.exception("Sync error: %s", exc)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
