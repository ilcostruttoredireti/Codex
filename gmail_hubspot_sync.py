#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox and syncs sender contacts to HubSpot automatically.
Avoids duplicates using email as unique key; updates missing fields on existing contacts.

Usage:
    python gmail_hubspot_sync.py

Environment variables:
    HUBSPOT_API_KEY         HubSpot private app token
    GMAIL_CREDENTIALS_FILE  Path to Google OAuth credentials JSON (default: credentials.json)
    GMAIL_TOKEN_FILE        Path to cached OAuth token (default: token.json)
    LOOKBACK_HOURS          Hours to look back on first run (default: 24)
"""

import os
import json
import re
import email.utils
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
GMAIL_CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
STATE_FILE = Path(__file__).parent / ".sync_state.json"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Local parts that indicate an automated/role address — not a real person
_AUTOMATED_RE = re.compile(
    r"^(no.?reply|noreply|nobody|mailer-daemon|postmaster|do.?not.?reply|"
    r"bounce|bounces|notification|notifications|newsletter|alerts?|"
    r"unsubscribe|digest|auto|autoresponder|devnull|blackhole|"
    r"accounts?services?|premium|daemon)$",
    re.IGNORECASE,
)

# Local parts that are role addresses (keep as firstname placeholder)
_ROLE_NAMES = {
    "info", "contact", "hello", "hi", "team", "sales", "marketing",
    "billing", "admin", "help", "service", "news", "press", "media",
    "commerciale", "commercial", "formazione", "support", "sa",
    "account", "accountservices",
}

# HubSpot field: closest standard value for "came via Gmail"
HS_LEAD_SOURCE = "OTHER"
HS_LEAD_SOURCE_DETAIL = "Inbound Gmail"


# --------------------------------------------------------------------------- #
# State helpers
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Gmail helpers
# --------------------------------------------------------------------------- #

def _get_gmail_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _get_header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_new_inbox_messages(service, since_history_id: Optional[str]) -> tuple[list[dict], str]:
    """
    Returns (messages, new_history_id).
    Uses Gmail History API when possible for efficiency; falls back to search.
    """
    messages: list[dict] = []

    def _get_msg(msg_id: str) -> dict:
        return service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()

    if since_history_id:
        try:
            resp = service.users().history().list(
                userId="me",
                startHistoryId=since_history_id,
                labelId="INBOX",
                historyTypes=["messageAdded"],
            ).execute()
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    messages.append(_get_msg(added["message"]["id"]))
        except Exception:
            since_history_id = None  # history ID expired, fall back

    if not since_history_id:
        query = f"in:inbox newer_than:{LOOKBACK_HOURS}h -from:me"
        resp = service.users().messages().list(
            userId="me", q=query, maxResults=100
        ).execute()
        for ref in resp.get("messages", []):
            messages.append(_get_msg(ref["id"]))

    profile = service.users().getProfile(userId="me").execute()
    return messages, profile["historyId"]


# --------------------------------------------------------------------------- #
# Contact parsing helpers
# --------------------------------------------------------------------------- #

def parse_sender(raw_from: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a raw From header."""
    name, addr = email.utils.parseaddr(raw_from)
    return name.strip(), addr.strip().lower()


def is_automated(local_part: str) -> bool:
    return bool(_AUTOMATED_RE.match(local_part))


def extract_name(display_name: str, local_part: str) -> tuple[Optional[str], Optional[str]]:
    """Return (firstname, lastname). May both be None if not determinable."""
    if display_name:
        parts = display_name.split(None, 1)
        return parts[0], (parts[1] if len(parts) > 1 else None)

    # Try to get first name from local part (e.g. riccardo@...)
    cleaned = re.sub(r"[-._+]", " ", local_part).strip()
    parts = [p for p in cleaned.split() if p]
    if parts:
        candidate = parts[0].lower()
        if candidate not in _ROLE_NAMES and len(candidate) > 2 and candidate.isalpha():
            return candidate.capitalize(), None
    return None, None


def company_from_domain(domain: str) -> str:
    """Derive a readable company name from domain (best-effort)."""
    # strip known TLDs (.com, .it, .co.uk …)
    base = domain.rsplit(".", 1)[0]
    if "." in base:
        base = base.split(".")[-1]
    name = re.sub(r"[-_]", " ", base)
    return name.title()


# --------------------------------------------------------------------------- #
# HubSpot API helpers
# --------------------------------------------------------------------------- #

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email_addr: str) -> Optional[dict]:
    url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email_addr}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    r = requests.post(url, headers=_hs_headers(), json=payload, timeout=10)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    r = requests.post(
        "https://api.hubapi.com/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    r = requests.patch(
        f"https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def hs_add_note(contact_id: str, body: str) -> None:
    """Add a timeline note and associate it with the contact."""
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    r = requests.post(
        "https://api.hubapi.com/crm/v3/objects/notes",
        headers=_hs_headers(),
        json={"properties": {"hs_note_body": body, "hs_timestamp": str(timestamp_ms)}},
        timeout=10,
    )
    r.raise_for_status()
    note_id = r.json()["id"]

    # Associate note ↔ contact
    requests.put(
        f"https://api.hubapi.com/crm/v3/objects/notes/{note_id}"
        f"/associations/contact/{contact_id}/note_to_contact",
        headers=_hs_headers(),
        timeout=10,
    )


# --------------------------------------------------------------------------- #
# Core sync logic
# --------------------------------------------------------------------------- #

def process_sender(
    raw_from: str, subject: str, date_str: str
) -> dict:
    """
    Sync one email sender to HubSpot.

    Returns:
        {
            "status":     "CREATO" | "AGGIORNATO" | "IGNORATO",
            "email":      str,
            "hubspot_id": str | None,
            "reason":     str,   # only when IGNORATO
        }
    """
    display_name, email_addr = parse_sender(raw_from)
    if not email_addr or "@" not in email_addr:
        return {"status": "IGNORATO", "email": raw_from, "hubspot_id": None, "reason": "invalid address"}

    local_part, domain = email_addr.split("@", 1)

    if is_automated(local_part):
        return {"status": "IGNORATO", "email": email_addr, "hubspot_id": None, "reason": "automated sender"}

    firstname, lastname = extract_name(display_name, local_part)
    company = company_from_domain(domain)

    desired: dict = {
        "email": email_addr,
        "company": company,
        "hs_lead_source": HS_LEAD_SOURCE,
    }
    if firstname:
        desired["firstname"] = firstname
    if lastname:
        desired["lastname"] = lastname

    activity_note = (
        f"📧 Email ricevuta via Gmail\n"
        f"Mittente: {raw_from}\n"
        f"Oggetto: {subject}\n"
        f"Data: {date_str}\n"
        f"Tag: {HS_LEAD_SOURCE_DETAIL}"
    )

    existing = hs_find_contact(email_addr)

    if existing:
        contact_id = str(existing["id"])
        existing_props = existing.get("properties", {})

        # Only patch fields that are currently blank
        update = {
            k: v
            for k, v in desired.items()
            if k != "email" and not existing_props.get(k)
        }

        if update:
            hs_update_contact(contact_id, update)
            status = "AGGIORNATO"
        else:
            status = "IGNORATO"

        hs_add_note(contact_id, activity_note)
        return {"status": status, "email": email_addr, "hubspot_id": contact_id}

    # New contact
    result = hs_create_contact(desired)
    contact_id = str(result["id"])
    hs_add_note(contact_id, activity_note)
    return {"status": "CREATO", "email": email_addr, "hubspot_id": contact_id}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main() -> None:
    print(f"=== Gmail → HubSpot Sync  [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}] ===\n")

    if not HUBSPOT_API_KEY:
        raise SystemExit("HUBSPOT_API_KEY is not set.")

    state = load_state()
    processed_ids: set = set(state.get("processed_message_ids", []))

    service = _get_gmail_service()
    messages, new_history_id = fetch_new_inbox_messages(service, state.get("last_history_id"))

    results: list[dict] = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed_ids:
            continue

        raw_from = _get_header(msg, "From")
        subject = _get_header(msg, "Subject") or "(no subject)"
        date_str = _get_header(msg, "Date") or ""

        result = process_sender(raw_from, subject, date_str)
        result["gmail_message_id"] = msg_id
        results.append(result)
        processed_ids.add(msg_id)

        icon = {"CREATO": "✅", "AGGIORNATO": "🔄", "IGNORATO": "⏭️ "}[result["status"]]
        hs_id = result.get("hubspot_id") or "-"
        reason = f"  ({result.get('reason', '')})" if result.get("reason") else ""
        print(f"  {icon} [{result['status']}]  {result['email']}  →  ID: {hs_id}{reason}")

    # Persist state
    state["last_history_id"] = new_history_id
    state["processed_message_ids"] = list(processed_ids)[-1000:]
    save_state(state)

    # Summary
    total = len(results)
    creati = sum(1 for r in results if r["status"] == "CREATO")
    aggiornati = sum(1 for r in results if r["status"] == "AGGIORNATO")
    ignorati = sum(1 for r in results if r["status"] == "IGNORATO")

    print(f"\n── Riepilogo ──────────────────────────────")
    print(f"  Email processate : {total}")
    print(f"  ✅ Creati        : {creati}")
    print(f"  🔄 Aggiornati    : {aggiornati}")
    print(f"  ⏭️  Ignorati      : {ignorati}")
    print(f"────────────────────────────────────────────")


if __name__ == "__main__":
    main()
