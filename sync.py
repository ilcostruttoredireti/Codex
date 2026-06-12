#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot.

Usage:
    python sync.py                  # Process emails from last 24 hours
    python sync.py --watch          # Continuous monitoring (polls every 60s)
    python sync.py --since 7d       # Last 7 days
    python sync.py --dry-run        # Parse only, no HubSpot writes
    python sync.py --no-notes       # Skip timeline note creation
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

# ── dependencies ──────────────────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    sys.exit("Google libs missing — run: pip install -r requirements.txt")

try:
    import hubspot
    from hubspot.crm.contacts import (
        SimplePublicObjectInputForCreate,
        SimplePublicObjectInput,
        ApiException,
    )
    from hubspot.crm.contacts.models import (
        Filter,
        FilterGroup,
        PublicObjectSearchRequest,
    )
    from hubspot.crm.objects.notes import (
        SimplePublicObjectInputForCreate as NoteCreate,
    )
except ImportError:
    sys.exit("HubSpot libs missing — run: pip install -r requirements.txt")

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── configuration ─────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")
STATE_FILE = Path(".sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Sender patterns to skip automatically
IGNORED_LOCAL_RE = re.compile(
    r"^(no-?reply|noreply|donotreply|do-not-reply|mailer-?daemon|postmaster|"
    r"bounce|notifications?|alerts?|newsletter|info|support|help|contact|sales)$",
    re.IGNORECASE,
)

IGNORED_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "noreply.github.com",
    "mailer.hubspot.com",
}


# ── Gmail authentication ──────────────────────────────────────────────────────
def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                sys.exit(
                    "credentials.json not found.\n"
                    "Download it from Google Cloud Console → APIs & Services → "
                    "Credentials → OAuth 2.0 Client IDs."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ── HubSpot client ────────────────────────────────────────────────────────────
def get_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_ACCESS_TOKEN:
        sys.exit(
            "HUBSPOT_ACCESS_TOKEN env var not set.\n"
            "Create a Private App: HubSpot → Settings → Integrations → Private Apps.\n"
            "Required scopes: crm.objects.contacts.read, crm.objects.contacts.write, "
            "crm.objects.notes.write"
        )
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


# ── email parsing ─────────────────────────────────────────────────────────────
def parse_sender(from_header: str) -> Optional[dict]:
    """
    Parse a From: header into contact fields.
    Returns None for auto-generated / ignored senders.
    """
    display_name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    local, domain = email_addr.split("@", 1)

    if domain in IGNORED_DOMAINS:
        return None
    if IGNORED_LOCAL_RE.match(local):
        return None

    first_name, last_name = _split_display_name(display_name)
    company = _domain_to_company(domain)

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }


def _split_display_name(name: str) -> tuple[str, str]:
    name = name.strip().strip('"\'')
    if not name:
        return "", ""
    parts = name.split()
    return parts[0], " ".join(parts[1:]) if len(parts) > 1 else ""


def _domain_to_company(domain: str) -> str:
    """'acme-corp.com' → 'Acme Corp'"""
    base = re.sub(
        r"\.(com|net|org|io|co|uk|de|fr|it|es|jp|br|ca|au|in|biz|info)(\.[a-z]{2})?$",
        "",
        domain,
    )
    return base.replace("-", " ").replace("_", " ").title()


# ── state (processed message IDs) ────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"processed_ids": [], "last_run": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Gmail: fetch messages ─────────────────────────────────────────────────────
def fetch_new_messages(service, since_hours: int = 24) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    query = f"in:inbox after:{int(cutoff.timestamp())}"

    message_stubs = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        message_stubs.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    full_messages = []
    for stub in message_stubs:
        try:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=stub["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            full_messages.append(msg)
        except HttpError as exc:
            log.warning("Could not fetch message %s: %s", stub["id"], exc)

    return full_messages


def _get_header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# ── HubSpot: find / create / update ──────────────────────────────────────────
def find_contact(hs: hubspot.Client, email: str) -> Optional[object]:
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
        ],
        properties=["email", "firstname", "lastname", "company", "lead_source"],
        limit=1,
    )
    try:
        resp = hs.crm.contacts.search_api.do_search(body=req)
        return resp.results[0] if resp.total > 0 else None
    except ApiException as exc:
        log.error("HubSpot search error: %s", exc)
        return None


def create_contact(hs: hubspot.Client, sender: dict) -> Optional[str]:
    props: dict[str, str] = {
        "email": sender["email"],
        "lead_source": "Gmail",
        "hs_lead_status": "NEW",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", sender["email"], exc)
        return None


def update_contact(hs: hubspot.Client, contact_id: str, sender: dict, existing) -> bool:
    """Update only the fields that are currently blank."""
    ep = existing.properties
    updates: dict[str, str] = {}

    if not ep.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not ep.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not ep.get("company") and sender["company"]:
        updates["company"] = sender["company"]

    if not updates:
        return False

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error for id %s: %s", contact_id, exc)
        return False


def create_timeline_note(hs: hubspot.Client, contact_id: str, subject: str, date_str: str):
    """Attach an 'email received' note to the contact timeline."""
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"📧 Email ricevuta da Gmail\n"
        f"Oggetto: {subject or '(nessun oggetto)'}\n"
        f"Data: {date_str}\n"
        f"Tag: Inbound Gmail"
    )
    try:
        note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(
                properties={
                    "hs_timestamp": str(ts_ms),
                    "hs_note_body": body,
                }
            )
        )
        # Link note → contact (association type 202 = note-to-contact)
        hs.crm.associations.v4.basic_api.create(
            object_type="notes",
            object_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_spec=[
                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}
            ],
        )
    except ApiException as exc:
        log.warning("Could not create timeline note for contact %s: %s", contact_id, exc)


# ── process a batch of messages ───────────────────────────────────────────────
def process_messages(
    hs: hubspot.Client,
    messages: list[dict],
    processed_ids: set,
    create_notes: bool = True,
) -> list[dict]:
    results = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed_ids:
            continue

        from_header = _get_header(msg, "From")
        subject = _get_header(msg, "Subject")
        date_str = _get_header(msg, "Date")

        sender = parse_sender(from_header)
        if sender is None:
            processed_ids.add(msg_id)
            results.append(
                {"status": "Ignorato", "email": from_header or "(vuoto)", "hubspot_id": None}
            )
            continue

        existing = find_contact(hs, sender["email"])

        if existing:
            contact_id = existing.id
            updated = update_contact(hs, contact_id, sender, existing)
            status = "Aggiornato" if updated else "Ignorato"
        else:
            contact_id = create_contact(hs, sender)
            status = "Creato" if contact_id else "Errore"

        if contact_id and create_notes:
            create_timeline_note(hs, contact_id, subject, date_str)

        processed_ids.add(msg_id)
        entry = {"status": status, "email": sender["email"], "hubspot_id": contact_id}
        results.append(entry)
        log.info("%-12s | %-35s | HubSpot ID: %s", status, sender["email"], contact_id)

    return results


def print_report(results: list[dict]):
    icons = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭", "Errore": "❌"}
    counts = {k: sum(1 for r in results if r["status"] == k) for k in icons}
    counts["Ignorato"] = sum(1 for r in results if r["status"] == "Ignorato")

    print("\n" + "═" * 62)
    print("  REPORT — Gmail → HubSpot Contact Sync")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("─" * 62)
    print(f"  Totale processate : {len(results)}")
    print(f"  ✅ Creati          : {counts['Creato']}")
    print(f"  🔄 Aggiornati      : {counts['Aggiornato']}")
    print(f"  ⏭  Ignorati        : {counts['Ignorato']}")
    print(f"  ❌ Errori          : {counts['Errore']}")
    print("─" * 62)
    for r in results:
        icon = icons.get(r["status"], "·")
        print(f"  {icon}  {r['status']:<12}  {r['email']}")
        if r["hubspot_id"]:
            print(f"            └─ HubSpot ID: {r['hubspot_id']}")
    print("═" * 62 + "\n")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    ap.add_argument("--watch", action="store_true", help="Continuous polling mode")
    ap.add_argument(
        "--since", default="24h", metavar="WINDOW",
        help="How far back to look (e.g. 24h, 7d). Default: 24h"
    )
    ap.add_argument("--no-notes", action="store_true", help="Skip timeline notes")
    ap.add_argument("--dry-run", action="store_true", help="Parse only, no HubSpot writes")
    args = ap.parse_args()

    m = re.fullmatch(r"(\d+)([hd]?)", args.since.strip())
    if not m:
        sys.exit("Invalid --since. Use e.g. 24h or 7d.")
    value, unit = int(m.group(1)), m.group(2) or "h"
    since_hours = value if unit == "h" else value * 24

    gmail_svc = get_gmail_service()
    hs = None if args.dry_run else get_hubspot_client()

    state = load_state()
    processed_ids: set = set(state.get("processed_ids", []))

    log.info("Gmail → HubSpot sync started (since=%s, watch=%s, dry_run=%s)",
             args.since, args.watch, args.dry_run)

    def run_once():
        messages = fetch_new_messages(gmail_svc, since_hours=since_hours)
        log.info("Fetched %d inbox messages (last %s)", len(messages), args.since)

        if args.dry_run:
            for msg in messages:
                s = parse_sender(_get_header(msg, "From"))
                print(f"[DRY RUN] {s}")
            return

        results = process_messages(
            hs, messages, processed_ids, create_notes=not args.no_notes
        )
        print_report(results)
        state["processed_ids"] = list(processed_ids)[-5000:]
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

    run_once()

    if args.watch:
        log.info("Watch mode active — polling every %ds. Ctrl+C to stop.", POLL_INTERVAL)
        while True:
            time.sleep(POLL_INTERVAL)
            run_once()


if __name__ == "__main__":
    main()
