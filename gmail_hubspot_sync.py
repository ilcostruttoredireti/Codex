#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs sender contacts to HubSpot.
Handles both direct senders and original senders embedded in forwarded emails.

Usage:
    python gmail_hubspot_sync.py              # run sync
    python gmail_hubspot_sync.py --dry-run   # preview without writing
    python gmail_hubspot_sync.py --days 3    # look back N days (default: 1)

Environment variables required:
    HUBSPOT_ACCESS_TOKEN  - HubSpot Private App token
    GOOGLE_CREDENTIALS    - path to Google OAuth2 credentials JSON (default: credentials.json)
    GOOGLE_TOKEN          - path to stored token (default: state/gmail_token.json)
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
from pathlib import Path
from typing import Optional

# ── Google ──────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── HubSpot ─────────────────────────────────────────────────────────────────
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

# ── Constants ────────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("state/last_sync.json")

SKIP_SENDERS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications",
    "notification", "automated", "support", "info",
}
SKIP_DOMAINS_CONTAINS = {
    "facebookmail.com", "bounce.", "amazonses.com",
    "sendgrid.net", "mailchimp.com", "constantcontact.com",
}

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("gmail_hs_sync")


# ── State management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    STATE_FILE.parent.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_thread_ids": [], "last_run_utc": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Gmail ────────────────────────────────────────────────────────────────────

def build_gmail_service():
    creds_path = Path(os.environ.get("GOOGLE_CREDENTIALS", "credentials.json"))
    token_path = Path(os.environ.get("GOOGLE_TOKEN", "state/gmail_token.json"))
    token_path.parent.mkdir(exist_ok=True)

    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Google credentials file not found at {creds_path}. "
                    "Download it from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_message_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    body = ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        body += get_message_body(part)
    return body


def fetch_inbox_threads(gmail_svc, lookback_days: int, processed_ids: set) -> list[dict]:
    """Fetch inbox threads from the last N days, skipping already processed ones."""
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    after_str = since.strftime("%Y/%m/%d")
    query = f"in:inbox -from:me after:{after_str}"

    threads_raw = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = gmail_svc.users().threads().list(**kwargs).execute()
        for t in resp.get("threads", []):
            if t["id"] not in processed_ids:
                threads_raw.append(t["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info(f"Found {len(threads_raw)} new inbox threads (lookback: {lookback_days}d)")

    # Fetch full thread data
    threads = []
    for thread_id in threads_raw:
        try:
            thread = gmail_svc.users().threads().get(
                userId="me", id=thread_id, format="full"
            ).execute()
            threads.append(thread)
        except HttpError as e:
            log.warning(f"Could not fetch thread {thread_id}: {e}")
    return threads


# ── Sender extraction ─────────────────────────────────────────────────────────

def parse_from_header(from_str: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse 'Display Name <email@domain>' or 'email@domain'
    Returns (email, firstname, lastname).
    """
    from_str = from_str.strip()
    # Format: "Name" <email> or Name <email>
    m = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_str)
    if m:
        name = m.group(1).strip()
        email = m.group(2).strip().lower()
    elif "@" in from_str:
        email = from_str.lower()
        name = ""
    else:
        return None, None, None

    if not email or "@" not in email:
        return None, None, None

    firstname, lastname = split_name(name)
    return email, firstname, lastname


def split_name(name: str) -> tuple[Optional[str], Optional[str]]:
    if not name:
        return None, None
    parts = name.strip().split(None, 1)
    return parts[0] if parts else None, parts[1] if len(parts) > 1 else None


def is_junk_sender(email: str) -> bool:
    """Return True for automated/system/newsletter senders to skip."""
    if not email or "@" not in email:
        return True
    local, domain = email.lower().split("@", 1)
    if local in SKIP_SENDERS:
        return True
    for bad in SKIP_DOMAINS_CONTAINS:
        if bad in domain:
            return True
    return False


def extract_forwarded_senders(body: str) -> list[tuple[str, Optional[str], Optional[str]]]:
    """
    Extract original sender info from Italian-style forwarded emails.
    Pattern: Da "Display Name" email@domain
    Also handles: From: Display Name <email@domain>
    """
    found = {}

    # Italian webmail format: Da "Tuttestorie Stampa" tuttestoriestampa@gmail.com
    italian_pattern = re.compile(
        r'Da\s+"?([^"<\n\r]+?)"?\s+([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
        re.IGNORECASE,
    )
    for m in italian_pattern.finditer(body):
        name = m.group(1).strip()
        email = m.group(2).strip().lower()
        if email not in found and not is_junk_sender(email):
            firstname, lastname = split_name(name)
            found[email] = (email, firstname, lastname)

    # Standard From: header embedded in forwards
    from_pattern = re.compile(
        r'From:\s+"?([^"<\n\r]*?)"?\s*<([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>',
        re.IGNORECASE,
    )
    for m in from_pattern.finditer(body):
        name = m.group(1).strip()
        email = m.group(2).strip().lower()
        if email not in found and not is_junk_sender(email):
            firstname, lastname = split_name(name)
            found[email] = (email, firstname, lastname)

    return list(found.values())


def extract_company_from_domain(email: str) -> Optional[str]:
    """Guess company name from email domain (best-effort)."""
    domain = email.split("@")[1] if "@" in email else ""
    parts = domain.split(".")
    if len(parts) >= 2:
        # Use second-to-last part (e.g. 'google' from 'mail.google.com')
        name = parts[-2]
        # Convert kebab-case / underscores to title case
        return re.sub(r"[-_]", " ", name).title()
    return None


def collect_senders_from_thread(thread: dict) -> dict[str, tuple]:
    """
    Collect unique senders from all messages in a thread.
    Returns {email: (firstname, lastname, company)}.
    """
    senders: dict[str, tuple] = {}

    for msg in thread.get("messages", []):
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}

        # Direct sender
        from_header = headers.get("From", "")
        email, firstname, lastname = parse_from_header(from_header)
        if email and not is_junk_sender(email) and email not in senders:
            company = extract_company_from_domain(email)
            senders[email] = (firstname, lastname, company)

        # Forwarded original senders
        body = get_message_body(msg["payload"])
        for fw_email, fw_first, fw_last in extract_forwarded_senders(body):
            if fw_email not in senders:
                company = extract_company_from_domain(fw_email)
                senders[fw_email] = (fw_first, fw_last, company)

    return senders


# ── HubSpot ──────────────────────────────────────────────────────────────────

def build_hubspot_client():
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable not set.")
    return hubspot.Client.create(access_token=token)


def find_contact_by_email(hs: hubspot.Client, email: str) -> Optional[object]:
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        result = hs.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        return result.results[0] if result.results else None
    except ApiException as e:
        log.error(f"HubSpot search error for {email}: {e}")
        return None


def upsert_contact(
    hs: hubspot.Client,
    email: str,
    firstname: Optional[str],
    lastname: Optional[str],
    company: Optional[str],
    dry_run: bool,
) -> tuple[str, Optional[str]]:
    """
    Create or update a HubSpot contact.
    Returns (status, contact_id) where status is 'Creato' | 'Aggiornato' | 'Ignorato'.
    """
    existing = find_contact_by_email(hs, email)

    if existing:
        contact_id = existing.id
        ep = existing.properties
        updates = {}

        if firstname and not ep.get("firstname"):
            updates["firstname"] = firstname
        if lastname and not ep.get("lastname"):
            updates["lastname"] = lastname
        if company and not ep.get("company"):
            updates["company"] = company
        if not ep.get("leadsource"):
            updates["leadsource"] = "Gmail"

        if not updates:
            return "Ignorato", contact_id

        if not dry_run:
            try:
                hs.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input=SimplePublicObjectInput(properties=updates),
                )
            except ApiException as e:
                log.error(f"HubSpot update error for {email}: {e}")
                return "Errore", contact_id
        return "Aggiornato", contact_id

    else:
        props = {"email": email, "leadsource": "Gmail"}
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        if company:
            props["company"] = company

        if not dry_run:
            try:
                result = hs.crm.contacts.basic_api.create(
                    simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                        properties=props
                    )
                )
                return "Creato", result.id
            except ApiException as e:
                log.error(f"HubSpot create error for {email}: {e}")
                return "Errore", None
        return "Creato (dry-run)", None


# ── Main sync ─────────────────────────────────────────────────────────────────

def sync(lookback_days: int = 1, dry_run: bool = False) -> list[dict]:
    state = load_state()
    processed_ids = set(state.get("processed_thread_ids", []))

    gmail_svc = build_gmail_service()
    hs = build_hubspot_client()

    threads = fetch_inbox_threads(gmail_svc, lookback_days, processed_ids)

    results: list[dict] = []
    new_processed: list[str] = []

    for thread in threads:
        thread_id = thread["id"]
        senders = collect_senders_from_thread(thread)

        for email, (firstname, lastname, company) in senders.items():
            status, contact_id = upsert_contact(
                hs, email, firstname, lastname, company, dry_run
            )
            entry = {
                "status": status,
                "email": email,
                "hubspot_id": contact_id,
                "thread_id": thread_id,
            }
            results.append(entry)

            icon = {"Creato": "✓ CREATO", "Aggiornato": "↑ AGGIORNATO", "Ignorato": "= IGNORATO"}.get(
                status, f"? {status}"
            )
            log.info(f"{icon:16} | {email:45} | HS ID: {contact_id}")

        new_processed.append(thread_id)
        time.sleep(0.1)  # rate-limit courtesy pause

    # Persist state
    all_processed = list(processed_ids | set(new_processed))
    state["processed_thread_ids"] = all_processed[-2000:]  # keep last 2000
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        save_state(state)

    _print_summary(results, dry_run)
    return results


def _print_summary(results: list[dict], dry_run: bool) -> None:
    created = [r for r in results if "Creato" in r["status"]]
    updated = [r for r in results if r["status"] == "Aggiornato"]
    ignored = [r for r in results if r["status"] == "Ignorato"]
    errors = [r for r in results if r["status"] == "Errore"]

    print()
    print("=" * 70)
    print(f"  GMAIL → HUBSPOT SYNC  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if dry_run:
        print("  [DRY RUN - nessuna modifica salvata]")
    print("=" * 70)
    print(
        f"  Creati: {len(created):3}  |  "
        f"Aggiornati: {len(updated):3}  |  "
        f"Ignorati: {len(ignored):3}  |  "
        f"Errori: {len(errors):3}"
    )
    print("-" * 70)
    for r in results:
        hs_id = r["hubspot_id"] or "—"
        print(f"  {r['status']:12} | {r['email']:45} | ID: {hs_id}")
    print("=" * 70)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no writes")
    parser.add_argument("--days", type=int, default=1, help="Lookback window in days (default: 1)")
    args = parser.parse_args()

    sync(lookback_days=args.days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
