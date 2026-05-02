#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors the inbox for incoming emails and upserts sender contacts into HubSpot.

Usage:
    python gmail_hubspot_sync.py            # run once then loop every 5 minutes
    python gmail_hubspot_sync.py --once     # single pass and exit

Required environment variables:
    HUBSPOT_ACCESS_TOKEN  – Private App token with contacts read/write scope

Required files (Gmail OAuth):
    credentials.json      – Desktop OAuth client from Google Cloud Console
    token.json            – Created automatically on first run (browser auth)
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("sync_state.json")
POLL_INTERVAL = 300  # seconds between polling cycles

# Domains that belong to the sender themselves (not a company)
PERSONAL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "libero.it"}

# Prefixes/patterns that indicate a system/noreply address
NOREPLY_PATTERNS = re.compile(
    r"^(no.?reply|noreply|do.not.reply|mailer.daemon|postmaster|bounce|daemon|auto.?reply)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_gmail_service():
    creds = None
    token_path = Path("token.json")
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_own_email(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"].lower()


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_inbox_message_ids(service, last_history_id: Optional[str]) -> tuple[list[str], str]:
    """
    Return (list_of_message_ids, new_history_id).
    Uses Gmail History API for incremental sync; falls back to search on first run.
    """
    profile = service.users().getProfile(userId="me").execute()
    current_history_id = profile["historyId"]

    if last_history_id:
        try:
            resp = service.users().history().list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            ids = []
            for record in resp.get("history", []):
                for m in record.get("messagesAdded", []):
                    ids.append(m["message"]["id"])
            return ids, current_history_id
        except HttpError as exc:
            if exc.status_code == 404:
                log.warning("History ID expired — falling back to full search.")
            else:
                raise

    # First run: fetch last 7 days
    result = service.users().messages().list(
        userId="me",
        q="in:inbox newer_than:7d",
        maxResults=500,
    ).execute()
    ids = [m["id"] for m in result.get("messages", [])]
    return ids, current_history_id


def get_from_header(service, msg_id: str) -> Optional[str]:
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From"],
    ).execute()
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == "from":
            return h["value"]
    return None


# ---------------------------------------------------------------------------
# Parsing / enrichment helpers
# ---------------------------------------------------------------------------

def parse_from_header(from_header: str) -> tuple[str, str, str]:
    """
    Parse 'Name <email>' or bare 'email' into (display_name, email, domain).
    """
    match = re.match(r'"?([^"<@\n]+)"?\s*<([^>]+)>', from_header.strip())
    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        email = re.sub(r"[<>]", "", from_header).strip().lower()
        name = ""
    domain = email.split("@")[-1] if "@" in email else ""
    return name, email, domain


def is_noreply(email: str) -> bool:
    local = email.split("@")[0]
    return bool(NOREPLY_PATTERNS.match(local))


def split_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname)."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def infer_name_from_local(local: str) -> tuple[str, str]:
    """
    'beatrice.giongo' → ('Beatrice', 'Giongo')
    'maya_amenduni'   → ('Maya', 'Amenduni')
    """
    parts = re.split(r"[._\-]", local)
    if len(parts) == 2 and all(p.isalpha() for p in parts):
        return parts[0].capitalize(), parts[1].capitalize()
    return "", ""


def company_from_domain(domain: str) -> str:
    if not domain or domain in PERSONAL_DOMAINS:
        return ""
    # strip TLD(s): gallerianazionalemarche.it → Gallerianazionalemarche
    stem = domain.rsplit(".", 1)[0]
    # handle subdomains: bn-na.ufficiostampa.cultura.gov → cultura gov
    parts = stem.split(".")
    stem = parts[-1] if len(parts) > 1 else parts[0]
    return stem.replace("-", " ").title()


def enrich_sender(display_name: str, email: str, domain: str) -> dict:
    """Build a clean contact dict from parsed sender data."""
    firstname, lastname = ("", "")

    if display_name and display_name != email:
        firstname, lastname = split_name(display_name)

    if not firstname:
        local = email.split("@")[0]
        firstname, lastname = infer_name_from_local(local)

    company = company_from_domain(domain)

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def build_hubspot_client() -> hubspot.Client:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN is not set.")
    return hubspot.Client.create(access_token=token)


def find_contact_by_email(hs: hubspot.Client, email: str) -> Optional[object]:
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source", "hs_analytics_source_data_1"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0] if resp.results else None


def create_contact(hs: hubspot.Client, data: dict) -> str:
    props = {"email": data["email"]}
    if data["firstname"]:
        props["firstname"] = data["firstname"]
    if data["lastname"]:
        props["lastname"] = data["lastname"]
    if data["company"]:
        props["company"] = data["company"]
    props["hs_analytics_source"] = "EMAIL_MARKETING"

    created = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
    )
    return created.id


def update_contact(hs: hubspot.Client, contact_id: str, existing_props: dict, new_data: dict) -> bool:
    """Only write fields that are currently blank. Returns True if any field was updated."""
    updates = {}
    for field in ("firstname", "lastname", "company"):
        if new_data.get(field) and not existing_props.get(field):
            updates[field] = new_data[field]
    if not existing_props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    if not updates:
        return False

    hs.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return True


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_message_ids(service, hs: hubspot.Client, msg_ids: list[str], own_email: str) -> list[dict]:
    results = []
    seen: set[str] = set()

    for msg_id in msg_ids:
        from_header = get_from_header(service, msg_id)
        if not from_header:
            continue

        display_name, email, domain = parse_from_header(from_header)

        if email in seen:
            continue
        seen.add(email)

        if email == own_email or is_noreply(email) or not email:
            log.debug("Skipping %s", email)
            continue

        data = enrich_sender(display_name, email, domain)

        try:
            existing = find_contact_by_email(hs, email)
            if existing:
                ep = existing.properties
                updated = update_contact(hs, existing.id, ep, data)
                status = "Aggiornato" if updated else "Ignorato"
                contact_id = existing.id
            else:
                contact_id = create_contact(hs, data)
                status = "Creato"
        except ApiException as exc:
            log.error("HubSpot API error for %s: %s", email, exc)
            status = "Errore"
            contact_id = None

        entry = {"status": status, "email": email, "hubspot_id": contact_id}
        results.append(entry)
        log.info("%-12s | %-45s | %s", status, email, contact_id or "N/A")

    return results


def sync_once(service, hs: hubspot.Client, state: dict, own_email: str) -> list[dict]:
    last_history_id = state.get("last_history_id")
    msg_ids, new_history_id = fetch_inbox_message_ids(service, last_history_id)
    log.info("Fetched %d message IDs to process.", len(msg_ids))
    results = process_message_ids(service, hs, msg_ids, own_email)
    state["last_history_id"] = new_history_id
    save_state(state)
    return results


def print_report(results: list[dict]):
    if not results:
        print("\nNessun nuovo mittente da processare.")
        return
    col_w = (12, 45, 22)
    sep = "+" + "+".join("-" * (w + 2) for w in col_w) + "+"
    fmt = "| {:<{}} | {:<{}} | {:<{}} |".format
    print("\n" + sep)
    print(fmt("Stato", col_w[0], "Email Contatto", col_w[1], "ID HubSpot", col_w[2]))
    print(sep)
    for r in results:
        print(fmt(r["status"], col_w[0], r["email"], col_w[1], r["hubspot_id"] or "N/A", col_w[2]))
    print(sep)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = " | ".join(f"{s}: {n}" for s, n in counts.items())
    print(f"\nRiepilogo: {len(results)} mittenti elaborati — {summary}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run a single sync pass and exit")
    args = parser.parse_args()

    service = build_gmail_service()
    hs = build_hubspot_client()
    own_email = get_own_email(service)
    log.info("Account Gmail monitorato: %s", own_email)

    state = load_state()

    while True:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        log.info("=== Sync avviato: %s ===", ts)
        results = sync_once(service, hs, state, own_email)
        print_report(results)

        if args.once:
            break

        log.info("Prossima esecuzione tra %d secondi...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
