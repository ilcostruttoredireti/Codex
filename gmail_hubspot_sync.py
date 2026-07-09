"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and syncs new senders as HubSpot contacts.

Usage:
    python gmail_hubspot_sync.py                 # run once
    python gmail_hubspot_sync.py --days 7        # look back N days (default: 1)
    python gmail_hubspot_sync.py --watch 300     # poll every N seconds
"""

import os
import re
import json
import time
import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")
STATE_FILE = Path(".sync_state.json")

HUBSPOT_API_BASE = "https://api.hubapi.com"
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN", "")

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Patterns that identify automated / no-reply senders to skip
AUTOMATED_PREFIXES = {
    "no-reply", "noreply", "nobody", "donotreply", "do-not-reply",
    "notification", "notifications", "newsletter", "mailer",
    "digest", "updates", "bounce", "postmaster", "daemon",
    "unsubscribe", "automated", "robot", "system", "alert",
    "automate", "info-noreply", "reply-noreply",
}

AUTOMATED_DOMAINS = {
    "facebookmail.com", "twittermail.com", "linkedin.com",
    "googlemail.com", "googleapis.com", "amazonses.com",
    "mailchimp.com", "sendgrid.net", "constantcontact.com",
    "mandrill.com", "mailgun.org",
}

# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------


def get_gmail_service():
    """Return an authenticated Gmail API service."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def search_inbox_messages(service, after_timestamp: int) -> list[dict]:
    """Return all inbox messages received after *after_timestamp* (Unix epoch)."""
    query = f"in:inbox -from:me after:{after_timestamp}"
    messages = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message_headers(service, msg_id: str) -> dict:
    """Fetch From/Subject headers for a single message (minimal payload)."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata",
             metadataHeaders=["From", "Subject", "Date"])
        .execute()
    )
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers


# ---------------------------------------------------------------------------
# Sender parsing
# ---------------------------------------------------------------------------


def parse_sender(from_header: str) -> tuple[str, str | None]:
    """Return (email, display_name | None) from a raw From header."""
    m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', from_header.strip())
    if m:
        return m.group(2).strip().lower(), m.group(1).strip()
    return from_header.strip().lower(), None


def split_name(display_name: str | None, email: str) -> tuple[str | None, str | None]:
    """Return (firstname, lastname) from display name or email local-part."""
    if display_name:
        parts = display_name.split(None, 1)
        return parts[0], parts[1] if len(parts) > 1 else None
    local = email.split("@")[0]
    parts = re.split(r"[._\-]", local)
    if len(parts) >= 2 and parts[0].lower() not in AUTOMATED_PREFIXES:
        return parts[0].capitalize(), parts[1].capitalize()
    return local.capitalize(), None


def company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from an email domain."""
    base = domain.rsplit(".", 1)[0]          # strip TLD
    base = re.sub(r"[.\-_]", " ", base)
    return base.title()


def is_automated(email: str) -> bool:
    """Return True for no-reply / automated sender addresses."""
    local, _, domain = email.partition("@")
    local = local.lower()
    domain = domain.lower()
    if any(domain.endswith(d) for d in AUTOMATED_DOMAINS):
        return True
    if any(local.startswith(p) or local == p for p in AUTOMATED_PREFIXES):
        return True
    return False


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def find_contact_by_email(email: str) -> dict | None:
    """Search HubSpot for a contact matching *email*. Returns the contact dict or None."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    r = requests.post(url, headers=_hs_headers(), json=body, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def create_contact(props: dict) -> str:
    """Create a new HubSpot contact and return its ID."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
    r = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def update_contact(contact_id: str, props: dict) -> None:
    """Patch an existing HubSpot contact with *props*."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
    r = requests.patch(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    r.raise_for_status()


def build_contact_props(
    email: str,
    display_name: str | None,
    domain: str,
) -> dict:
    """Build a HubSpot property dict for a new / updated contact."""
    firstname, lastname = split_name(display_name, email)
    company = company_from_domain(domain)
    props: dict = {
        "email": email,
        "hs_lead_source": "OTHER_CAMPAIGNS",   # closest standard value for Gmail
        "hs_analytics_source": CONTACT_SOURCE,  # free-text source label
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def sync_contact(email: str, display_name: str | None) -> tuple[str, str]:
    """
    Ensure a contact exists in HubSpot for *email*.

    Returns (status, hubspot_id) where status is one of:
        "Creato"    – contact did not exist and was created
        "Aggiornato" – contact existed; missing fields were filled in
        "Ignorato"  – contact existed and nothing needed updating
    """
    domain = email.split("@")[1] if "@" in email else ""
    existing = find_contact_by_email(email)

    if existing is None:
        props = build_contact_props(email, display_name, domain)
        contact_id = create_contact(props)
        log.info("Creato   %s  →  HubSpot ID %s", email, contact_id)
        return "Creato", contact_id

    contact_id = existing["id"]
    eprops = existing.get("properties", {})
    updates: dict = {}

    # Fill in any fields that are currently empty
    if not eprops.get("firstname") and not eprops.get("lastname"):
        fn, ln = split_name(display_name, email)
        if fn:
            updates["firstname"] = fn
        if ln:
            updates["lastname"] = ln

    if not eprops.get("company"):
        updates["company"] = company_from_domain(domain)

    if not eprops.get("hs_lead_source"):
        updates["hs_lead_source"] = "OTHER_CAMPAIGNS"

    if updates:
        update_contact(contact_id, updates)
        log.info("Aggiornato %s  →  HubSpot ID %s  (campi: %s)", email, contact_id, list(updates))
        return "Aggiornato", contact_id

    log.info("Ignorato   %s  →  HubSpot ID %s  (già completo)", email, contact_id)
    return "Ignorato", contact_id


# ---------------------------------------------------------------------------
# State management (tracks processed message IDs to avoid double-processing)
# ---------------------------------------------------------------------------


def load_state() -> set:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("processed_msg_ids", []))
    return set()


def save_state(processed: set) -> None:
    # Keep at most 10 000 IDs to cap file size
    ids = list(processed)[-10_000:]
    STATE_FILE.write_text(json.dumps({"processed_msg_ids": ids}, indent=2))


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------


def run_sync(lookback_days: int = 1) -> list[dict]:
    """
    Run one sync pass.

    Returns a list of result dicts:
        {"stato": str, "email": str, "hubspot_id": str}
    """
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_TOKEN environment variable not set")

    service = get_gmail_service()
    after_ts = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())

    log.info("Cercando email arrivate negli ultimi %d giorni…", lookback_days)
    messages = search_inbox_messages(service, after_ts)
    log.info("Trovati %d messaggi da analizzare", len(messages))

    processed = load_state()
    seen_emails: set[str] = set()
    results: list[dict] = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed:
            continue

        try:
            headers = get_message_headers(service, msg_id)
        except Exception as exc:
            log.warning("Impossibile leggere messaggio %s: %s", msg_id, exc)
            processed.add(msg_id)
            continue

        from_header = headers.get("From", "")
        if not from_header:
            processed.add(msg_id)
            continue

        email, display_name = parse_sender(from_header)

        # Skip automated senders
        if is_automated(email):
            processed.add(msg_id)
            continue

        # Deduplicate within this run (one contact per unique email)
        if email in seen_emails:
            processed.add(msg_id)
            continue
        seen_emails.add(email)

        try:
            stato, hubspot_id = sync_contact(email, display_name)
        except requests.HTTPError as exc:
            log.error("Errore HubSpot per %s: %s", email, exc)
            stato, hubspot_id = "Errore", ""

        results.append({"stato": stato, "email": email, "hubspot_id": hubspot_id})
        processed.add(msg_id)

    save_state(processed)
    return results


def print_report(results: list[dict]) -> None:
    """Print a formatted summary table."""
    if not results:
        print("\nNessun nuovo contatto da elaborare.\n")
        return

    print("\n" + "=" * 72)
    print(f"{'Stato':<14} {'Email':<40} {'HubSpot ID'}")
    print("-" * 72)
    for r in results:
        print(f"{r['stato']:<14} {r['email']:<40} {r['hubspot_id']}")
    print("=" * 72)

    totals = {}
    for r in results:
        totals[r["stato"]] = totals.get(r["stato"], 0) + 1
    summary = "  |  ".join(f"{k}: {v}" for k, v in sorted(totals.items()))
    print(f"\nTotale: {len(results)} contatti  →  {summary}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--days", type=int, default=1,
        help="Quanti giorni indietro cercare email (default: 1)"
    )
    parser.add_argument(
        "--watch", type=int, default=0, metavar="SECONDS",
        help="Polling continuo ogni N secondi (0 = esegui una volta sola)"
    )
    args = parser.parse_args()

    if args.watch > 0:
        log.info("Modalità watch: polling ogni %d secondi", args.watch)
        while True:
            results = run_sync(lookback_days=args.days)
            print_report(results)
            log.info("Prossimo controllo tra %d secondi…", args.watch)
            time.sleep(args.watch)
    else:
        results = run_sync(lookback_days=args.days)
        print_report(results)


if __name__ == "__main__":
    main()
