#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for new emails, extracts sender contacts,
and syncs them to HubSpot (create or update, no duplicates).

Usage:
  python gmail_hubspot_sync.py              # process new emails once
  python gmail_hubspot_sync.py --watch 60   # poll every 60 seconds
  python gmail_hubspot_sync.py --reset      # clear state and reprocess all

Requirements:
  pip install -r requirements.txt
  Set HUBSPOT_ACCESS_TOKEN in environment (or .env file)
  Place Google OAuth credentials.json in the same directory
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(__file__).parent / "sync_state.json"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
TOKEN_FILE = Path(__file__).parent / "token.json"
HUBSPOT_BASE = "https://api.hubapi.com"

# Local/free email domains — don't derive company name from these
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "protonmail.com", "live.com",
    "libero.it", "tin.it", "alice.it", "virgilio.it", "tiscali.it",
}

# Addresses that are automated/system senders — skip them
_BOT_PATTERNS = {
    "noreply", "no-reply", "donotreply", "notifications",
    "mailer-daemon", "postmaster", "bounce", "unsubscribe",
    "newsletter", "info-noreply", "auto",
}


# ─── Data models ──────────────────────────────────────────────────────────────

@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""

    @classmethod
    def from_raw(cls, email: str, display_name: str = "") -> Optional["SenderContact"]:
        email = email.strip().lower()
        if not email or "@" not in email:
            return None
        domain = email.split("@", 1)[1]
        firstname, lastname = _split_name(display_name)
        company = _company_from_domain(domain, display_name)
        return cls(email=email, firstname=firstname, lastname=lastname,
                   company=company, domain=domain)


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored" | "skipped"
    email: str
    hubspot_id: Optional[str] = None
    message_id: str = ""

    def __str__(self) -> str:
        icons = {"created": "✅", "updated": "🔄", "ignored": "⏭️ ", "skipped": "🚫"}
        icon = icons.get(self.status, "❓")
        hs = f"  [HS: {self.hubspot_id}]" if self.hubspot_id else ""
        return f"  {icon}  {self.status.upper():<8}  {self.email}{hs}"


# ─── Name / domain helpers ────────────────────────────────────────────────────

def _split_name(display_name: str) -> tuple[str, str]:
    name = display_name.strip().strip('"').strip("'")
    # If the display name looks like an email address, ignore it
    if "@" in name:
        return "", ""
    parts = name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0].title(), ""
    return parts[0].title(), " ".join(parts[1:]).title()


def _company_from_domain(domain: str, display_name: str = "") -> str:
    """Derive a human-readable company name from an email domain."""
    if domain in _GENERIC_DOMAINS:
        return ""
    # If display name is clearly an org name (not an email), use it
    clean_name = display_name.strip().strip('"')
    if clean_name and "@" not in clean_name and len(clean_name.split()) > 1:
        return clean_name  # e.g. "Ufficio Stampa Comune di San Severino Marche"
    # Derive from domain: take the most meaningful label
    labels = domain.split(".")
    skip = {"www", "mail", "smtp", "mx", "webmail"}
    meaningful = [l for l in labels if l not in skip and len(l) > 2]
    if not meaningful:
        return domain
    # Prefer the first label that is not a generic TLD
    tlds = {"com", "net", "org", "gov", "edu", "it", "co", "uk", "de", "fr",
            "eu", "io", "ai", "app", "dev", "mc", "info", "biz"}
    candidates = [l for l in meaningful if l not in tlds]
    base = candidates[0] if candidates else meaningful[0]
    return base.replace("-", " ").title()


# ─── State management ─────────────────────────────────────────────────────────

def _load_state() -> set[str]:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return set(data.get("processed_ids", []))
    return set()


def _save_state(processed_ids: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"processed_ids": sorted(processed_ids)}, indent=2)
    )


# ─── Gmail ────────────────────────────────────────────────────────────────────

def _gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                sys.exit(
                    f"\n[ERROR] {CREDENTIALS_FILE} not found.\n"
                    "Download OAuth 2.0 credentials from Google Cloud Console:\n"
                    "  https://console.cloud.google.com/apis/credentials\n"
                    "Save the file as 'credentials.json' in the same directory.\n"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_from_header(value: str) -> tuple[str, str]:
    """Parse RFC 2822 From header → (display_name, email)."""
    m = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>', value.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r'^([\w.+%\-]+@[\w.\-]+\.[a-zA-Z]{2,})$', value.strip())
    if m:
        return "", m.group(1)
    return "", value.strip()


def _body_text(message: dict) -> str:
    """Recursively extract text/plain from a Gmail message payload."""
    def _extract(part: dict) -> str:
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
        for sub in part.get("parts", []):
            result = _extract(sub)
            if result:
                return result
        return ""
    return _extract(message.get("payload", {}))


# Patterns to extract original sender from forwarded messages (Italian + English)
_FWD_PATTERNS = [
    # Da "Display Name" email@domain.com
    (r'Da\s+"([^"]+)"\s+([\w.+%\-]+@[\w.\-]+\.[a-zA-Z]{2,})', 2),
    # Da: "Display Name" <email@domain.com>
    (r'Da:\s+"?([^"<\n]+?)"?\s+<([\w.+%\-]+@[\w.\-]+\.[a-zA-Z]{2,})>', 2),
    # Da: email@domain.com
    (r'Da:\s+([\w.+%\-]+@[\w.\-]+\.[a-zA-Z]{2,})', 1),
    # From: "Display Name" <email@domain.com>
    (r'From:\s+"?([^"<\n]+?)"?\s+<([\w.+%\-]+@[\w.\-]+\.[a-zA-Z]{2,})>', 2),
    # From: email@domain.com
    (r'From:\s+([\w.+%\-]+@[\w.\-]+\.[a-zA-Z]{2,})', 1),
]


def _parse_forwarded_sender(body: str) -> tuple[str, str]:
    """Return (display_name, email) of original sender from forwarded body."""
    for pattern, groups in _FWD_PATTERNS:
        m = re.search(pattern, body)
        if m:
            if groups == 2:
                return m.group(1).strip(), m.group(2).strip()
            return "", m.group(1).strip()
    return "", ""


def _is_forwarded(subject: str) -> bool:
    return subject.lower().startswith(("fw:", "fwd:", "i:", "tr:", "inoltro:"))


def extract_sender(message: dict) -> Optional[SenderContact]:
    """Return the relevant SenderContact from a Gmail message."""
    headers = message.get("payload", {}).get("headers", [])
    from_header = _header(headers, "From")
    subject = _header(headers, "Subject")

    display_name, email = _parse_from_header(from_header)

    if _is_forwarded(subject):
        body = _body_text(message)
        fwd_name, fwd_email = _parse_forwarded_sender(body)
        if fwd_email:
            display_name = fwd_name or display_name
            email = fwd_email

    return SenderContact.from_raw(email, display_name)


def fetch_new_messages(service, processed_ids: set[str]) -> list[dict]:
    """Return full message objects for unprocessed inbox emails."""
    resp = service.users().messages().list(
        userId="me",
        q="in:inbox -in:sent -in:draft",
        maxResults=100,
    ).execute()

    all_msgs = resp.get("messages", [])
    new_msg_ids = [m["id"] for m in all_msgs if m["id"] not in processed_ids]

    full = []
    for msg_id in new_msg_ids:
        msg = service.users().messages().get(
            userId="me", messageId=msg_id, format="full"
        ).execute()
        full.append(msg)

    return full


# ─── HubSpot ──────────────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()
    if not token:
        sys.exit(
            "\n[ERROR] HUBSPOT_ACCESS_TOKEN not set.\n"
            "Create a Private App in HubSpot (Settings → Integrations → Private Apps)\n"
            "with scopes: crm.objects.contacts.read, crm.objects.contacts.write, crm.objects.notes.write\n"
            "Then set: export HUBSPOT_ACCESS_TOKEN=your_token\n"
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def hs_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns contact dict or None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{
            "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
        }],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    resp = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(contact: SenderContact) -> dict:
    """Create a new HubSpot contact. Returns the created object."""
    props: dict[str, str] = {
        "email": contact.email,
        "hs_lead_status": "NEW",
    }
    if contact.firstname:
        props["firstname"] = contact.firstname
    if contact.lastname:
        props["lastname"] = contact.lastname
    if contact.company:
        props["company"] = contact.company

    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        json={"properties": props},
        headers=_hs_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, contact: SenderContact, existing: dict) -> bool:
    """Fill in blank fields on an existing contact. Returns True if changed."""
    existing_props = existing.get("properties", {})
    updates: dict[str, str] = {}

    if contact.firstname and not existing_props.get("firstname"):
        updates["firstname"] = contact.firstname
    if contact.lastname and not existing_props.get("lastname"):
        updates["lastname"] = contact.lastname
    if contact.company and not existing_props.get("company"):
        updates["company"] = contact.company

    if not updates:
        return False

    resp = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        json={"properties": updates},
        headers=_hs_headers(),
        timeout=15,
    )
    resp.raise_for_status()
    return True


def hs_add_note(contact_id: str, subject: str, sender_email: str, date_str: str) -> None:
    """Add an inbound-email note to the contact's timeline."""
    body = (
        f"📥 Inbound Gmail\n"
        f"Da: {sender_email}\n"
        f"Oggetto: {subject}\n"
        f"Data: {date_str}\n"
        f"Tag: Inbound Gmail"
    )
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": ts,
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": 202,   # Note → Contact
            }],
        }],
    }
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/notes",
        json=payload,
        headers=_hs_headers(),
        timeout=15,
    )
    resp.raise_for_status()


# ─── Core sync logic ──────────────────────────────────────────────────────────

def _should_skip(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return any(p in local for p in _BOT_PATTERNS)


def sync_one(contact: SenderContact, message: dict) -> SyncResult:
    """Sync a single contact to HubSpot and log a timeline note."""
    msg_id = message["id"]
    headers = message.get("payload", {}).get("headers", [])
    subject = _header(headers, "Subject") or "(no subject)"
    date_str = _header(headers, "Date") or datetime.now().isoformat()

    existing = hs_find_contact(contact.email)

    if existing:
        contact_id = existing["id"]
        changed = hs_update_contact(contact_id, contact, existing)
        status = "updated" if changed else "ignored"
    else:
        created = hs_create_contact(contact)
        contact_id = created["id"]
        status = "created"

    try:
        hs_add_note(contact_id, subject, contact.email, date_str)
    except Exception as exc:
        print(f"  [WARN] Note non creata per {contact.email}: {exc}")

    return SyncResult(status=status, email=contact.email,
                      hubspot_id=contact_id, message_id=msg_id)


def run_sync(service) -> list[SyncResult]:
    """One full sync pass: fetch new messages, sync contacts, persist state."""
    processed_ids = _load_state()
    messages = fetch_new_messages(service, processed_ids)

    results: list[SyncResult] = []

    for msg in messages:
        msg_id = msg["id"]
        processed_ids.add(msg_id)   # mark regardless of outcome

        contact = extract_sender(msg)
        if contact is None:
            continue
        if _should_skip(contact.email):
            results.append(SyncResult(status="skipped", email=contact.email,
                                      message_id=msg_id))
            continue

        try:
            result = sync_one(contact, msg)
            results.append(result)
        except requests.HTTPError as exc:
            print(f"  [ERR] HubSpot error for {contact.email}: {exc}")
        except Exception as exc:
            print(f"  [ERR] Unexpected error for {contact.email}: {exc}")

    _save_state(processed_ids)
    return results


def _print_results(results: list[SyncResult]) -> None:
    if not results:
        print(f"  Nessun nuovo contatto da sincronizzare.")
        return

    created = sum(1 for r in results if r.status == "created")
    updated = sum(1 for r in results if r.status == "updated")
    ignored = sum(1 for r in results if r.status == "ignored")
    skipped = sum(1 for r in results if r.status == "skipped")

    width = 64
    print(f"\n{'─' * width}")
    print(f"  {datetime.now():%Y-%m-%d %H:%M:%S}  |  "
          f"✅ {created} creati  🔄 {updated} aggiornati  "
          f"⏭️  {ignored} invariati  🚫 {skipped} saltati")
    print(f"{'─' * width}")
    for r in results:
        print(str(r))
    print(f"{'─' * width}\n")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincronizza mittenti Gmail con contatti HubSpot."
    )
    parser.add_argument(
        "--watch", type=int, metavar="SECONDI",
        help="Polling continuo ogni N secondi (es. --watch 60)",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Azzera lo stato e rielabora tutte le email",
    )
    args = parser.parse_args()

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("[INFO] Stato azzerato. Tutte le email verranno rielaborate.")

    print("[INFO] Autenticazione Gmail...")
    service = _gmail_service()
    print("[INFO] Autenticato. Avvio sincronizzazione...")

    def once() -> None:
        results = run_sync(service)
        _print_results(results)

    if args.watch:
        print(f"[INFO] Monitoraggio attivo ogni {args.watch}s. Premi Ctrl+C per fermare.\n")
        while True:
            try:
                once()
                time.sleep(args.watch)
            except KeyboardInterrupt:
                print("\n[INFO] Monitoraggio interrotto.")
                break
    else:
        once()


if __name__ == "__main__":
    main()
