#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
-----------------------------
Monitors the Gmail inbox and automatically syncs senders as HubSpot contacts.

Usage:
    export HUBSPOT_API_KEY="pat-..."
    export GOOGLE_CREDENTIALS_FILE="credentials.json"   # OAuth2 desktop-app JSON
    export OWN_EMAIL_1="your@email.com"
    python gmail_hubspot_sync.py

OAuth2 first-run: opens a browser for Google authorisation, then saves
a token to token.json (path configurable via GOOGLE_TOKEN_FILE).

For headless/scheduled runs, authenticate once interactively and check in
token.json (or use a service account with domain-wide delegation).
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Configuration ─────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")
STATE_FILE = Path(os.environ.get("STATE_FILE", "processed_emails.json"))
MAX_EMAILS_PER_RUN = int(os.environ.get("MAX_EMAILS_PER_RUN", "100"))

# Senders to skip unconditionally (automated / system)
SKIP_PATTERNS = [
    r"mailer-daemon@",
    r"(no-?reply|do-?not-?reply)@",
    r"postmaster@",
    r"notification@.*facebookmail\.com",
    r"analytics-noreply@google\.com",
    r"^@googlemail\.com$",      # bounce notifications only
    r"@.*\.pec\.it$",           # Italian PEC delivery receipts
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)


# ── Gmail ─────────────────────────────────────────────────────────────────────
def get_gmail_service():
    creds = None
    if Path(GOOGLE_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        Path(GOOGLE_TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, max_results=MAX_EMAILS_PER_RUN):
    """Fetch recent INBOX messages (excludes Sent, Spam, Trash)."""
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
        .execute()
    )
    return result.get("messages", [])


def get_message_sender(service, msg_id: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a message's From header."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From"])
        .execute()
    )
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    raw_from = headers.get("From", "")
    name, addr = parseaddr(raw_from)
    return name.strip(), addr.strip().lower()


# ── Filtering ─────────────────────────────────────────────────────────────────
def should_skip(email_addr: str, own_emails: set[str]) -> bool:
    if email_addr in own_emails:
        return True
    return any(re.search(p, email_addr, re.IGNORECASE) for p in SKIP_PATTERNS)


# ── Name / company helpers ────────────────────────────────────────────────────
def split_name(display_name: str) -> tuple[str, str]:
    """'Giulia Rossi' → ('Giulia', 'Rossi');  'Ufficio Stampa' → ('Ufficio', 'Stampa')."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def company_from_domain(email_addr: str) -> str:
    """
    Infer a human-readable company name from the email domain.
    'press@latestata.it'  →  'Latestata'
    'info@museo-reale.it' →  'Museo Reale'
    """
    domain = email_addr.split("@")[-1]
    parts = domain.split(".")
    # strip common prefixes like 'www', 'mail', 'm'
    if len(parts) >= 2:
        name_part = parts[-2]
    else:
        name_part = domain
    return name_part.replace("-", " ").replace("_", " ").title()


# ── HubSpot ───────────────────────────────────────────────────────────────────
_HUBSPOT_BASE = "https://api.hubapi.com"


def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}


def hubspot_search_contact(email_addr: str) -> dict | None:
    """Return the first matching HubSpot contact or None."""
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email_addr}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    r = requests.post(
        f"{_HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hubspot_create_contact(props: dict) -> dict:
    r = requests.post(
        f"{_HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hubspot_update_contact(contact_id: str, props: dict) -> dict:
    r = requests.patch(
        f"{_HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hubspot_create_note(contact_id: str, body: str) -> dict:
    """Log a note activity on a contact (timeline entry)."""
    r = requests.post(
        f"{_HUBSPOT_BASE}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json={
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,  # note → contact
                        }
                    ],
                }
            ],
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ── State tracking ────────────────────────────────────────────────────────────
def load_state() -> set[str]:
    """Return the set of Gmail message IDs already processed."""
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_state(processed: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(processed), indent=2))


# ── Core sync logic ───────────────────────────────────────────────────────────
SyncResult = dict  # {"status": str, "email": str, "hubspot_id": str}


def sync_contact(display_name: str, email_addr: str) -> SyncResult:
    """
    Ensure the sender exists in HubSpot.
    - If new  → create contact + timeline note
    - If exists with missing fields → fill them in
    - If already complete → skip
    """
    existing = hubspot_search_contact(email_addr)
    firstname, lastname = split_name(display_name)
    company = company_from_domain(email_addr)

    if existing:
        contact_id = existing["id"]
        p = existing.get("properties", {})

        updates: dict = {}
        if firstname and not p.get("firstname"):
            updates["firstname"] = firstname
        if lastname and not p.get("lastname"):
            updates["lastname"] = lastname
        if not p.get("company"):
            updates["company"] = company

        if updates:
            hubspot_update_contact(contact_id, updates)
            return {"status": "updated", "email": email_addr, "hubspot_id": contact_id}

        return {"status": "skipped", "email": email_addr, "hubspot_id": contact_id}

    # ── New contact ───────────────────────────────────────────────────────────
    props: dict = {
        "email": email_addr,
        "company": company,
        "hs_lead_status": "NEW",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname

    contact = hubspot_create_contact(props)
    contact_id = contact["id"]

    # Timeline note: source + tag
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hubspot_create_note(
        contact_id,
        f"📧 Email inbound ricevuta via Gmail\n"
        f"Fonte contatto: Gmail\n"
        f"Tag: Inbound Gmail\n"
        f"Data: {now_str}",
    )

    return {"status": "created", "email": email_addr, "hubspot_id": contact_id}


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(results: list[SyncResult]) -> None:
    sep = "─" * 70
    print(f"\n{sep}")
    print(f"{'STATO':<12} {'EMAIL':<42} {'HUBSPOT ID'}")
    print(sep)
    for r in results:
        print(f"{r['status'].upper():<12} {r['email']:<42} {r['hubspot_id']}")
    print(sep)
    created = sum(1 for r in results if r["status"] == "created")
    updated = sum(1 for r in results if r["status"] == "updated")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    print(
        f"Totale elaborati: {len(results)}"
        f" | Creati: {created}"
        f" | Aggiornati: {updated}"
        f" | Ignorati: {skipped}"
    )


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    if not HUBSPOT_API_KEY:
        raise SystemExit("ERROR: HUBSPOT_API_KEY environment variable not set.")

    own_emails: set[str] = {
        e.strip().lower()
        for e in [
            os.environ.get("OWN_EMAIL_1", ""),
            os.environ.get("OWN_EMAIL_2", ""),
        ]
        if e.strip()
    }

    service = get_gmail_service()
    processed = load_state()
    messages = fetch_inbox_messages(service)

    log.info("Fetched %d inbox messages; %d already processed.", len(messages), len(processed))

    results: list[SyncResult] = []
    seen_this_run: set[str] = set()

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed:
            continue

        try:
            display_name, email_addr = get_message_sender(service, msg_id)
        except Exception as exc:
            log.warning("Could not parse sender for %s: %s", msg_id, exc)
            processed.add(msg_id)
            continue

        if not email_addr:
            processed.add(msg_id)
            continue

        if should_skip(email_addr, own_emails):
            log.debug("SKIP  %s (filtered)", email_addr)
            processed.add(msg_id)
            continue

        # Deduplicate within this run so we only call HubSpot once per address
        if email_addr in seen_this_run:
            processed.add(msg_id)
            continue
        seen_this_run.add(email_addr)

        try:
            result = sync_contact(display_name, email_addr)
            results.append(result)
            log.info(
                "%-10s | %-42s | %s",
                result["status"].upper(),
                result["email"],
                result["hubspot_id"],
            )
        except requests.HTTPError as exc:
            log.error("HubSpot error for %s: %s – %s", email_addr, exc, exc.response.text)
        except Exception as exc:
            log.error("Unexpected error for %s: %s", email_addr, exc)
        finally:
            processed.add(msg_id)

    save_state(processed)
    print_report(results)


if __name__ == "__main__":
    main()
