#!/usr/bin/env python3
"""Gmail → HubSpot Contact Sync

Monitors Gmail INBOX continuously and syncs every inbound sender to HubSpot,
creating new contacts or updating existing ones. Avoids duplicates by keying
on email address.

Usage:
    python gmail_hubspot_sync.py              # run once then exit
    python gmail_hubspot_sync.py --loop       # poll forever (POLL_INTERVAL_SECONDS)

Environment variables (or .env file):
    HUBSPOT_API_KEY          HubSpot private-app token
    GMAIL_CREDENTIALS_FILE   path to Google OAuth credentials.json  (default: credentials.json)
    GMAIL_TOKEN_FILE         path to cached OAuth token              (default: token.json)
    POLL_INTERVAL_SECONDS    seconds between polling passes          (default: 60)
    PROCESSED_IDS_FILE       path to JSON state file                 (default: .processed_ids.json)
    GMAIL_MAX_RESULTS        messages to fetch per pass              (default: 50)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
import os

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
HUBSPOT_API_KEY: str = os.getenv("HUBSPOT_API_KEY", "")
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
PROCESSED_IDS_FILE: Path = Path(os.getenv("PROCESSED_IDS_FILE", ".processed_ids.json"))
GMAIL_MAX_RESULTS: int = int(os.getenv("GMAIL_MAX_RESULTS", "50"))

CONTACT_SOURCE = "OTHER_CAMPAIGNS"   # HubSpot enum closest to "Gmail"
SOURCE_LABEL = "Gmail"
INBOUND_NOTE_TAG = "Inbound Gmail"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
]

# Email domains that belong to generic providers (no meaningful company to extract)
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "live.com", "icloud.com", "me.com", "protonmail.com", "pm.me",
}

# Senders to skip entirely
SKIP_PATTERNS = [
    "noreply", "no-reply", "mailer-daemon", "postmaster",
    "donotreply", "do-not-reply", "bounce", "notifications",
    "alerts", "-noreply@", "noreply@", "auto-reply",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Data models ────────────────────────────────────────────────────────────────
@dataclass
class Sender:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    website: str = ""

    @property
    def fullname(self) -> str:
        return f"{self.firstname} {self.lastname}".strip()


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    contact_id: Optional[str] = None

    def __str__(self) -> str:
        cid = self.contact_id or "—"
        return f"[{self.status:<10}] {self.email:<45}  ID HubSpot: {cid}"


# ── Gmail helpers ──────────────────────────────────────────────────────────────
def _get_gmail_service():
    """Return an authenticated Gmail API service object."""
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        log.error("google-api-python-client not installed. Run: pip install -r requirements.txt")
        sys.exit(1)

    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_from_header(raw: str) -> tuple[str, str]:
    """Parse 'Display Name <email@example.com>' → (display_name, email)."""
    m = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>$', raw.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return "", raw.strip().lower()


def _name_from_local(local: str) -> tuple[str, str]:
    """Attempt to split 'mario.rossi' or 'mario_rossi' into first/last name."""
    parts = re.split(r"[._\-]", local)
    parts = [p for p in parts if len(p) > 1 and not p.isdigit()]
    if len(parts) >= 2:
        return parts[0].capitalize(), parts[1].capitalize()
    return parts[0].capitalize() if parts else ("", "")


def _domain_to_company(domain: str) -> str:
    """Convert a domain to a human-readable company name."""
    if domain in GENERIC_DOMAINS:
        return ""
    # Strip known sub-prefixes that don't add meaning
    name = re.sub(r"^(www|mail|webmail|info|press|redazione)\.", "", domain, flags=re.I)
    # Remove TLD
    name = re.sub(
        r"\.(com|it|net|org|eu|io|co\.uk|gov|edu|onlus|aps|ets)$", "", name, flags=re.I
    )
    # Clean separators → spaces → title-case
    return re.sub(r"[-_.]", " ", name).title()


def _extract_sender(message: dict) -> Optional[Sender]:
    raw_from = _header(message, "From")
    if not raw_from:
        return None

    display_name, email = _parse_from_header(raw_from)
    if not email or "@" not in email:
        return None

    if any(p in email.lower() for p in SKIP_PATTERNS):
        return None

    local, domain = email.split("@", 1)

    if display_name:
        parts = display_name.split()
        firstname = parts[0] if parts else ""
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
    else:
        firstname, lastname = _name_from_local(local)

    company = _domain_to_company(domain)
    website = f"https://{domain}" if domain not in GENERIC_DOMAINS else ""

    return Sender(
        email=email,
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
        website=website,
    )


def fetch_inbox_messages(service, max_results: int = 50) -> list[dict]:
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
        .execute()
    )
    messages = []
    for m in result.get("messages", []):
        full = (
            service.users()
            .messages()
            .get(userId="me", id=m["id"], format="full")
            .execute()
        )
        messages.append(full)
    return messages


# ── HubSpot helpers ────────────────────────────────────────────────────────────
_HS_BASE = "https://api.hubapi.com"


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> Optional[dict]:
    """Return the first HubSpot contact matching the email, or None."""
    url = f"{_HS_BASE}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "website"],
        "limit": 1,
    }
    r = httpx.post(url, headers=_hs_headers(), json=body, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: Sender) -> dict:
    """Create a new HubSpot contact and return the created object."""
    url = f"{_HS_BASE}/crm/v3/objects/contacts"
    props: dict = {
        "email": sender.email,
        "hs_analytics_source": CONTACT_SOURCE,
    }
    if sender.firstname:
        props["firstname"] = sender.firstname
    if sender.lastname:
        props["lastname"] = sender.lastname
    if sender.company:
        props["company"] = sender.company
    if sender.website:
        props["website"] = sender.website

    r = httpx.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, sender: Sender, existing: dict) -> bool:
    """Update only empty fields on an existing contact. Returns True if updated."""
    ep = existing.get("properties", {})

    def _fill(key: str, val: str) -> Optional[tuple[str, str]]:
        if val and not ep.get(key):
            return key, val
        return None

    updates = dict(filter(None, [
        _fill("firstname", sender.firstname),
        _fill("lastname", sender.lastname),
        _fill("company", sender.company),
        _fill("website", sender.website),
    ]))

    if not updates:
        return False

    url = f"{_HS_BASE}/crm/v3/objects/contacts/{contact_id}"
    r = httpx.patch(url, headers=_hs_headers(), json={"properties": updates}, timeout=15)
    r.raise_for_status()
    return True


def hs_log_note(contact_id: str, sender: Sender, subject: str, snippet: str):
    """Attach an inbound-email note to the contact's timeline."""
    url = f"{_HS_BASE}/crm/v3/objects/notes"
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body_html = (
        f"<strong>{INBOUND_NOTE_TAG}</strong><br>"
        f"Da: {sender.fullname or sender.email} &lt;{sender.email}&gt;<br>"
        f"Oggetto: {subject}<br>"
        f"Estratto: {snippet[:400]}"
    )
    payload = {
        "properties": {
            "hs_note_body": body_html,
            "hs_timestamp": now_ms,
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,
                    }
                ],
            }
        ],
    }
    r = httpx.post(url, headers=_hs_headers(), json=payload, timeout=15)
    r.raise_for_status()


# ── State management ───────────────────────────────────────────────────────────
def _load_processed() -> set[str]:
    if PROCESSED_IDS_FILE.exists():
        return set(json.loads(PROCESSED_IDS_FILE.read_text()))
    return set()


def _save_processed(ids: set[str]):
    PROCESSED_IDS_FILE.write_text(json.dumps(sorted(ids), indent=2))


# ── Core sync logic ────────────────────────────────────────────────────────────
def _sync_message(message: dict, processed: set[str]) -> Optional[SyncResult]:
    msg_id = message["id"]
    if msg_id in processed:
        return None

    sender = _extract_sender(message)
    processed.add(msg_id)

    if not sender:
        return SyncResult("Ignorato", _header(message, "From") or msg_id)

    subject = _header(message, "Subject") or "(senza oggetto)"
    snippet = message.get("snippet", "")

    try:
        existing = hs_find_contact(sender.email)

        if existing:
            contact_id = existing["id"]
            updated = hs_update_contact(contact_id, sender, existing)
            hs_log_note(contact_id, sender, subject, snippet)
            status = "Aggiornato" if updated else "Aggiornato"
            return SyncResult(status, sender.email, contact_id)
        else:
            created = hs_create_contact(sender)
            contact_id = created["id"]
            hs_log_note(contact_id, sender, subject, snippet)
            return SyncResult("Creato", sender.email, contact_id)

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            # Duplicate email — contact created by a concurrent request
            existing = hs_find_contact(sender.email)
            if existing:
                return SyncResult("Aggiornato", sender.email, existing["id"])
        log.warning("HubSpot error for %s: %s", sender.email, exc)
        return SyncResult("Ignorato", sender.email)
    except Exception as exc:
        log.warning("Errore su %s: %s", sender.email, exc)
        return SyncResult("Ignorato", sender.email)


def run_pass(service) -> list[SyncResult]:
    """Execute one full sync pass over the inbox."""
    processed = _load_processed()
    messages = fetch_inbox_messages(service, max_results=GMAIL_MAX_RESULTS)

    results: list[SyncResult] = []
    for msg in messages:
        result = _sync_message(msg, processed)
        if result and result.status != "Ignorato":
            results.append(result)
            log.info(str(result))

    _save_processed(processed)
    return results


def _print_summary(results: list[SyncResult]):
    created = sum(1 for r in results if r.status == "Creato")
    updated = sum(1 for r in results if r.status == "Aggiornato")
    ignored = sum(1 for r in results if r.status == "Ignorato")
    print(f"\n{'─'*65}")
    print(f"  Creati: {created}   Aggiornati: {updated}   Ignorati: {ignored}")
    print(f"{'─'*65}")
    if results:
        print(f"\n{'Stato':<12} {'Email':<45}  {'ID HubSpot'}")
        print(f"{'─'*12} {'─'*45}  {'─'*15}")
        for r in results:
            print(f"{r.status:<12} {r.email:<45}  {r.contact_id or '—'}")


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Sync Gmail inbox senders to HubSpot")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL,
                        help=f"Polling interval in seconds (default: {POLL_INTERVAL})")
    args = parser.parse_args()

    if not HUBSPOT_API_KEY:
        log.error("HUBSPOT_API_KEY non configurato. Imposta la variabile d'ambiente o il file .env")
        sys.exit(1)

    service = _get_gmail_service()

    if args.loop:
        log.info("Modalità loop attiva — intervallo: %ds", args.interval)
        while True:
            log.info("── Avvio ciclo di sync ─────────────────────────────────────")
            results = run_pass(service)
            _print_summary(results)
            log.info("Prossimo ciclo tra %ds", args.interval)
            time.sleep(args.interval)
    else:
        log.info("Esecuzione singola")
        results = run_pass(service)
        _print_summary(results)


if __name__ == "__main__":
    main()
