#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
-----------------------------
Monitors Gmail for incoming emails and automatically creates or updates
HubSpot contacts from senders. Uses email address as the unique key to
avoid duplicates.

Usage:
    python gmail_hubspot_sync.py               # continuous polling
    python gmail_hubspot_sync.py --run-once    # single pass, then exit
    python gmail_hubspot_sync.py --help
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("sync_state.json")

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_USER_ID = os.getenv("GMAIL_USER_ID", "me")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
INITIAL_LOOKBACK_DAYS = int(os.getenv("INITIAL_LOOKBACK_DAYS", "1"))

_ignored_raw = os.getenv("IGNORED_DOMAINS", "noreply.com,no-reply.com,mailer.com")
IGNORED_DOMAINS: set[str] = {d.strip().lower() for d in _ignored_raw.split(",") if d.strip()}

_own_raw = os.getenv("OWN_DOMAINS", "")
OWN_DOMAINS: set[str] = {d.strip().lower() for d in _own_raw.split(",") if d.strip()}

# Patterns that indicate automated/transactional senders (skip them)
SKIP_EMAIL_PATTERNS = re.compile(
    r"^(noreply|no-reply|donotreply|do-not-reply|mailer-daemon|postmaster|bounce|"
    r"notifications?|alerts?|support-tickets?|autoresponder|newsletter)@",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# State management (tracks last processed message ID per run)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"processed_message_ids": [], "last_history_id": None}


def save_state(state: dict) -> None:
    # Keep only the last 10 000 processed IDs to bound file size
    state["processed_message_ids"] = state["processed_message_ids"][-10_000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_gmail_service():
    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)
    creds_path = Path(GMAIL_CREDENTIALS_FILE)

    if not creds_path.exists():
        log.error(
            "Gmail credentials file not found: %s\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials.",
            GMAIL_CREDENTIALS_FILE,
        )
        sys.exit(1)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_message_headers(service, msg_id: str) -> dict | None:
    """Return a dict of relevant headers for a single message."""
    try:
        msg = service.users().messages().get(
            userId=GMAIL_USER_ID,
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        headers["_id"] = msg["id"]
        return headers
    except HttpError as e:
        log.warning("Could not fetch message %s: %s", msg_id, e)
        return None


def fetch_new_message_ids(service, state: dict) -> list[str]:
    """
    Return message IDs that arrived since the last run.
    Uses Gmail history API when possible; falls back to date query on first run.
    """
    last_history_id = state.get("last_history_id")

    if last_history_id:
        try:
            history = (
                service.users()
                .history()
                .list(
                    userId=GMAIL_USER_ID,
                    startHistoryId=last_history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            new_ids: list[str] = []
            for record in history.get("history", []):
                for added in record.get("messagesAdded", []):
                    new_ids.append(added["message"]["id"])
            # Update history cursor
            state["last_history_id"] = history.get("historyId", last_history_id)
            return new_ids
        except HttpError as e:
            if e.resp.status == 404:
                log.warning("History ID expired, falling back to date query.")
                state["last_history_id"] = None
            else:
                raise

    # First run / fallback: query by date
    cutoff = datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
    query = f"in:inbox after:{cutoff.strftime('%Y/%m/%d')}"
    result = service.users().messages().list(userId=GMAIL_USER_ID, q=query, maxResults=500).execute()
    messages = result.get("messages", [])

    # Seed the history cursor for future runs
    profile = service.users().getProfile(userId=GMAIL_USER_ID).execute()
    state["last_history_id"] = profile.get("historyId")

    return [m["id"] for m in messages]


# ---------------------------------------------------------------------------
# Contact extraction
# ---------------------------------------------------------------------------

def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Parse a From header into (display_name, email, domain).
    Returns ("", "", "") if the address is invalid.
    """
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.strip().lower()
    if not email_addr or "@" not in email_addr:
        return "", "", ""
    domain = email_addr.split("@", 1)[1]
    return display_name.strip(), email_addr, domain


def split_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into ('First', 'Last'). Handles single-word names."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD and capitalises)."""
    # Remove common sub-domains
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in ("mail", "smtp", "mx", "email", "m"):
        parts = parts[1:]
    # Use the second-level domain as the company name
    company = parts[0].replace("-", " ").title() if parts else ""
    return company


def should_skip(email_addr: str, domain: str) -> bool:
    if domain in IGNORED_DOMAINS:
        return True
    if domain in OWN_DOMAINS:
        return True
    if SKIP_EMAIL_PATTERNS.match(email_addr):
        return True
    return False


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def build_hubspot_client():
    if not HUBSPOT_ACCESS_TOKEN:
        log.error(
            "HUBSPOT_ACCESS_TOKEN is not set.\n"
            "Create a Private App in HubSpot and copy its token into .env"
        )
        sys.exit(1)
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_hubspot_contact(hs_client, email_addr: str) -> dict | None:
    """Return the first HubSpot contact matching email_addr, or None."""
    f = Filter(property_name="email", operator="EQ", value=email_addr)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    try:
        resp = hs_client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if resp.results:
            return resp.results[0].to_dict()
    except ApiException as e:
        log.error("HubSpot search error: %s", e)
    return None


def create_hubspot_contact(hs_client, props: dict) -> str | None:
    """Create a new contact. Returns the new contact ID or None on failure."""
    obj = SimplePublicObjectInputForCreate(properties=props)
    try:
        resp = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        return resp.id
    except ApiException as e:
        log.error("HubSpot create error: %s", e)
    return None


def update_hubspot_contact(hs_client, contact_id: str, props: dict) -> bool:
    """Update an existing contact. Returns True on success."""
    from hubspot.crm.contacts import SimplePublicObjectInput

    obj = SimplePublicObjectInput(properties=props)
    try:
        hs_client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=obj,
        )
        return True
    except ApiException as e:
        log.error("HubSpot update error: %s", e)
    return False


def sync_contact(hs_client, display_name: str, email_addr: str, domain: str) -> tuple[str, str]:
    """
    Ensure the sender exists in HubSpot.
    Returns (status, contact_id) where status ∈ {"created", "updated", "ignored"}.
    """
    first, last = split_name(display_name)
    company = company_from_domain(domain)

    existing = find_hubspot_contact(hs_client, email_addr)

    if existing:
        contact_id = existing["id"]
        current_props = existing.get("properties", {})

        # Only patch fields that are currently blank in HubSpot
        updates: dict[str, str] = {}
        if first and not current_props.get("firstname"):
            updates["firstname"] = first
        if last and not current_props.get("lastname"):
            updates["lastname"] = last
        if company and not current_props.get("company"):
            updates["company"] = company
        if not current_props.get("hs_lead_source"):
            updates["hs_lead_source"] = "Gmail"

        if updates:
            ok = update_hubspot_contact(hs_client, contact_id, updates)
            return ("updated" if ok else "ignored"), contact_id
        return "ignored", contact_id

    # New contact
    new_props: dict[str, str] = {
        "email": email_addr,
        "hs_lead_source": "Gmail",
    }
    if first:
        new_props["firstname"] = first
    if last:
        new_props["lastname"] = last
    if company:
        new_props["company"] = company

    new_id = create_hubspot_contact(hs_client, new_props)
    if new_id:
        return "created", new_id
    return "ignored", ""


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

RESULT_EMOJI = {"created": "✅ CREATO", "updated": "🔄 AGGIORNATO", "ignored": "⏭  IGNORATO"}


def process_message(service, hs_client, msg_id: str, state: dict) -> None:
    if msg_id in state["processed_message_ids"]:
        return

    headers = get_message_headers(service, msg_id)
    if not headers:
        return

    from_header = headers.get("from", "")
    display_name, email_addr, domain = parse_sender(from_header)

    # Mark as processed immediately so we don't retry on transient errors
    state["processed_message_ids"].append(msg_id)

    if not email_addr:
        log.debug("Skipping message %s — no valid From address", msg_id)
        return

    if should_skip(email_addr, domain):
        log.info("%-12s | %-40s | (automated/skipped)", "IGNORATO", email_addr)
        return

    status, contact_id = sync_contact(hs_client, display_name, email_addr, domain)
    label = RESULT_EMOJI.get(status, status.upper())
    log.info("%-20s | %-40s | HubSpot ID: %s", label, email_addr, contact_id or "—")


def run_sync(service, hs_client, state: dict) -> None:
    new_ids = fetch_new_message_ids(service, state)
    if not new_ids:
        log.debug("No new messages.")
        return

    # Deduplicate against already-processed set for efficiency
    processed_set = set(state["processed_message_ids"])
    to_process = [mid for mid in new_ids if mid not in processed_set]
    if not to_process:
        log.debug("All %d message(s) already processed.", len(new_ids))
        return

    log.info("Processing %d new message(s)…", len(to_process))
    for msg_id in to_process:
        process_message(service, hs_client, msg_id, state)
    save_state(state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Process current inbox once and exit (no continuous polling).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL,
        metavar="SECONDS",
        help=f"Polling interval in seconds (default: {POLL_INTERVAL}).",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Clear local sync state and start fresh.",
    )
    args = parser.parse_args()

    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()
        log.info("Sync state reset.")

    log.info("Initialising Gmail service…")
    service = build_gmail_service()

    log.info("Initialising HubSpot client…")
    hs_client = build_hubspot_client()

    state = load_state()

    if args.run_once:
        log.info("Running single sync pass…")
        run_sync(service, hs_client, state)
        log.info("Done.")
        return

    log.info("Starting continuous sync (interval: %ds). Press Ctrl+C to stop.", args.interval)
    while True:
        try:
            run_sync(service, hs_client, state)
        except Exception as exc:
            log.error("Sync error: %s", exc, exc_info=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
