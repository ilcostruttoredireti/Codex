#!/usr/bin/env python3
"""
gmail_to_hubspot.py — Monitor Gmail inbox and sync sender contacts to HubSpot.

Requirements:
    pip install -r requirements.txt

Setup:
    1. Gmail OAuth2
       - Go to console.cloud.google.com → Create project → Enable Gmail API
       - Create OAuth2 Desktop credentials → download as credentials.json
       - First run will open a browser to authorise; token.json is saved for reuse

    2. HubSpot Private App
       - Settings → Integrations → Private Apps → Create
       - Scopes: crm.objects.contacts.read, crm.objects.contacts.write
       - Copy the access token

    3. Environment
       export HUBSPOT_ACCESS_TOKEN="pat-na1-..."

Usage:
    python gmail_to_hubspot.py           # continuous loop (Ctrl+C to stop)
    python gmail_to_hubspot.py --once    # single pass and exit
    python gmail_to_hubspot.py --dry-run # parse only, no HubSpot writes
"""

import argparse
import json
import logging
import os
import re
import time
from email.utils import parseaddr
from pathlib import Path

# ─── Configuration ─────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL", "300"))  # 5 min default
STATE_FILE = Path(os.environ.get("STATE_FILE", "processed_ids.json"))
LOG_FILE = Path(os.environ.get("LOG_FILE", "sync.log"))
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

# Regex patterns for senders to skip (bots, bounces, certified mail, etc.)
SKIP_PATTERNS = [
    r"^mailer-daemon@",
    r"^postmaster@",
    r"noreply",
    r"no-reply",
    r"analytics-noreply",
    r"^posta-certificata@",
    r"@legalmail\.it$",
    r"@bounce\.",
    r"@notifications\.",
    r"@.*\.bounces\.",
    r"daemon@",
    r"^do-not-reply@",
]

# Generic mail providers → don't derive company name from domain
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "libero.it", "tiscali.it",
    "alice.it", "virgilio.it", "tin.it", "fastwebnet.it",
}

# ───────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-10s  %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Gmail layer
# ═══════════════════════════════════════════════════════════════════════════════

def build_gmail_service():
    """Authenticate and return a Gmail API service object."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    token_path = Path("token.json")
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, processed_ids: set) -> list[dict]:
    """Return metadata for INBOX messages not already processed."""
    result = service.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=100
    ).execute()

    new_msgs = []
    for ref in result.get("messages", []):
        if ref["id"] in processed_ids:
            continue
        msg = service.users().messages().get(
            userId="me",
            id=ref["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        new_msgs.append({"id": ref["id"], "payload": msg})

    log.info("Found %d new message(s) in inbox.", len(new_msgs))
    return new_msgs


def extract_sender(msg: dict) -> dict | None:
    """
    Parse sender info from a Gmail message dict.
    Returns None if the sender should be skipped.
    """
    headers = {
        h["name"]: h["value"]
        for h in msg["payload"].get("payload", {}).get("headers", [])
    }
    raw_from = headers.get("From", "")
    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None

    for pattern in SKIP_PATTERNS:
        if re.search(pattern, email_addr, re.IGNORECASE):
            log.debug("Skipping automated sender: %s", email_addr)
            return None

    domain = email_addr.split("@")[1]
    company = _company_from_domain(domain)
    firstname, lastname = _split_name(display_name, email_addr)

    return {
        "email": email_addr,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def _company_from_domain(domain: str) -> str:
    if domain in GENERIC_DOMAINS:
        return ""
    parts = domain.split(".")
    # strip common subdomains
    if parts[0] in ("www", "mail", "info", "support"):
        parts = parts[1:]
    return parts[0].replace("-", " ").title() if parts else ""


def _split_name(display_name: str, email_addr: str) -> tuple[str, str]:
    """Return (firstname, lastname)."""
    if display_name:
        parts = display_name.strip().split(None, 1)
        first = _title(parts[0]) if parts else ""
        last = _title(parts[1]) if len(parts) > 1 else ""
        return first, last

    # fall back to local part of email
    local = email_addr.split("@")[0]
    separators = re.split(r"[._\-]", local)
    if len(separators) >= 2 and all(s.isalpha() for s in separators[:2]):
        return _title(separators[0]), _title(separators[1])

    return _title(local), ""


def _title(s: str) -> str:
    return s.strip().capitalize() if s else ""


# ═══════════════════════════════════════════════════════════════════════════════
# HubSpot layer
# ═══════════════════════════════════════════════════════════════════════════════

def build_hubspot_client():
    if not HUBSPOT_TOKEN:
        raise SystemExit(
            "HUBSPOT_ACCESS_TOKEN is not set. "
            "Export the token before running:\n"
            "  export HUBSPOT_ACCESS_TOKEN='pat-na1-...'"
        )
    from hubspot import HubSpot
    return HubSpot(access_token=HUBSPOT_TOKEN)


def find_contact(client, email: str):
    """Search HubSpot for a contact by email. Returns contact object or None."""
    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{"filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]}],
                "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
                "limit": 1,
            }
        )
        return result.results[0] if result.results else None
    except Exception as exc:
        log.warning("HubSpot search error (%s): %s", email, exc)
        return None


def create_contact(client, data: dict, dry_run: bool = False) -> str:
    """Create a new HubSpot contact. Returns the new contact ID."""
    props = _build_props(data)
    if dry_run:
        log.info("[DRY-RUN] Would create contact: %s", props)
        return "dry-run"
    try:
        from hubspot.crm.contacts import SimplePublicObjectInputForCreate
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except Exception as exc:
        log.error("Create failed (%s): %s", data["email"], exc)
        return ""


def update_contact(client, contact_id: str, existing_props: dict, data: dict,
                   dry_run: bool = False) -> bool:
    """Update only blank fields on an existing contact. Returns True if any change was made."""
    updates: dict[str, str] = {}

    for field in ("firstname", "lastname", "company"):
        if data.get(field) and not existing_props.get(field):
            updates[field] = data[field]

    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if not updates:
        return False

    if dry_run:
        log.info("[DRY-RUN] Would update contact %s: %s", contact_id, updates)
        return True

    try:
        from hubspot.crm.contacts import SimplePublicObjectInput
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except Exception as exc:
        log.error("Update failed (id=%s): %s", contact_id, exc)
        return False


def _build_props(data: dict) -> dict:
    props: dict[str, str] = {
        "email": data["email"],
        "hs_lead_source": "Gmail",
    }
    for field in ("firstname", "lastname", "company"):
        if data.get(field):
            props[field] = data[field]
    return props


# ═══════════════════════════════════════════════════════════════════════════════
# State persistence
# ═══════════════════════════════════════════════════════════════════════════════

def load_state() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(ids: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids), indent=2))


# ═══════════════════════════════════════════════════════════════════════════════
# Core processing
# ═══════════════════════════════════════════════════════════════════════════════

def run_pass(gmail_svc, hs_client, processed_ids: set, dry_run: bool = False) -> list[dict]:
    """One full inbox scan → HubSpot sync. Returns per-message result dicts."""
    messages = fetch_inbox_messages(gmail_svc, processed_ids)
    results: list[dict] = []

    for msg in messages:
        msg_id = msg["id"]
        processed_ids.add(msg_id)

        sender = extract_sender(msg)
        if not sender:
            results.append({"status": "IGNORATO", "email": "—", "hs_id": "—", "msg_id": msg_id})
            continue

        email = sender["email"]
        existing = find_contact(hs_client, email)

        if existing:
            ex_props = existing.properties or {}
            changed = update_contact(hs_client, existing.id, ex_props, sender, dry_run)
            status = "AGGIORNATO" if changed else "INVARIATO"
            hs_id = existing.id
        else:
            hs_id = create_contact(hs_client, sender, dry_run)
            status = "CREATO" if hs_id else "ERRORE"

        row = {"status": status, "email": email, "hs_id": hs_id, "msg_id": msg_id}
        results.append(row)
        log.info("%-12s  %-45s  hs_id=%-12s", status, email, hs_id)

    save_state(processed_ids)
    return results


def print_table(results: list[dict]) -> None:
    if not results:
        print("Nessun nuovo messaggio da processare.")
        return
    header = f"{'Stato':<12}  {'Email contatto':<45}  {'ID HubSpot'}"
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        print(f"{r['status']:<12}  {r['email']:<45}  {r['hs_id']}")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no HubSpot writes")
    args = parser.parse_args()

    log.info("Gmail → HubSpot sync starting  (dry_run=%s)", args.dry_run)

    gmail_svc = build_gmail_service()
    hs_client = None if args.dry_run else build_hubspot_client()
    processed_ids = load_state()

    if args.once or args.dry_run:
        results = run_pass(gmail_svc, hs_client, processed_ids, dry_run=args.dry_run)
        print_table(results)
        return

    log.info("Polling every %d seconds. Press Ctrl+C to stop.", POLL_INTERVAL_SECONDS)
    while True:
        try:
            results = run_pass(gmail_svc, hs_client, processed_ids)
            print_table(results)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
