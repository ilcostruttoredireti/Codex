#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Continuously monitors Gmail inbox and syncs sender contacts to HubSpot,
handling forwarded emails and avoiding duplicates.

Setup:
    pip install -r requirements.txt
    Copy .env.example → .env and fill in credentials.
    Place your Gmail OAuth credentials file at the path in GMAIL_CREDENTIALS_FILE.
    Run: python gmail_hubspot_sync.py
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

OWN_EMAILS: frozenset[str] = frozenset(
    filter(
        None,
        [
            os.getenv("OWN_EMAIL_1", "cristian.mameli.editore@gmail.com"),
            os.getenv("OWN_EMAIL_2", "redazione@latestata.it"),
            os.getenv("OWN_EMAIL_3", "pubblica.latestata@gmail.com"),
            os.getenv("OWN_EMAIL_4", ""),
        ],
    )
)

GENERIC_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "yahoo.com", "yahoo.it",
        "hotmail.com", "hotmail.it", "outlook.com",
        "libero.it", "tiscali.it", "virgilio.it",
        "icloud.com", "me.com", "mac.com",
    }
)

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns for sender extraction
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Matches "Da: Name <email>" or "From: Name <email>" (with optional colon)
_FWD_WITH_BRACKETS = re.compile(
    r"(?:^|\n)(?:Da|From):?\s+(.+?)\s+<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>",
    re.IGNORECASE | re.MULTILINE,
)

# Matches 'Da "Name" email' (Italian style, no angle brackets)
_FWD_QUOTED_NAME = re.compile(
    r'(?:^|\n)Da\s+"([^"]+)"\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE | re.MULTILINE,
)

# Matches "Da: email" or "From: email" with no display name
_FWD_EMAIL_ONLY = re.compile(
    r"(?:^|\n)(?:Da|From):?\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------


def _get_gmail_service():
    """Return an authenticated Gmail API service object."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds: Optional[Credentials] = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _fetch_inbox_threads(service) -> list[dict]:
    query = f"in:inbox -in:sent -in:draft newer_than:{LOOKBACK_DAYS}d"
    resp = (
        service.users()
        .threads()
        .list(userId="me", q=query, maxResults=50)
        .execute()
    )
    return resp.get("threads", [])


def _get_thread(service, thread_id: str) -> Optional[dict]:
    try:
        return service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    except Exception as exc:
        log.warning("Could not fetch thread %s: %s", thread_id, exc)
        return None


def _get_header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    for sub in part.get("parts", []):
        text = _decode_part(sub)
        if text:
            return text
    return ""


def _get_body(message: dict) -> str:
    payload = message.get("payload", {})
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload)
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            return _decode_part(part)
    return _decode_part(payload)


def _extract_senders(message: dict) -> list[tuple[str, str]]:
    """Return list of (display_name, email) for all senders in a message."""
    found: dict[str, str] = {}  # email → name

    # Direct From header
    raw_from = _get_header(message, "From")
    if raw_from:
        name, addr = parseaddr(raw_from)
        addr = addr.lower().strip()
        if addr and addr not in OWN_EMAILS:
            found[addr] = name.strip()

    # Scan body for forwarded sender lines
    body = _get_body(message)
    if body:
        for m in _FWD_WITH_BRACKETS.finditer(body):
            name_raw = m.group(1).strip(" \t-")
            addr = m.group(2).strip().lower()
            if addr and addr not in OWN_EMAILS:
                found.setdefault(addr, name_raw)

        for m in _FWD_QUOTED_NAME.finditer(body):
            name_raw = m.group(1).strip()
            addr = m.group(2).strip().lower()
            if addr and addr not in OWN_EMAILS:
                found.setdefault(addr, name_raw)

        for m in _FWD_EMAIL_ONLY.finditer(body):
            addr = m.group(1).strip().lower()
            if addr and addr not in OWN_EMAILS:
                found.setdefault(addr, "")

    return list(found.items())


# ---------------------------------------------------------------------------
# Contact data helpers
# ---------------------------------------------------------------------------


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


def _company_from_email(email: str) -> str:
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1].lower()
    if domain in GENERIC_DOMAINS:
        return ""
    # Turn "rec-media.it" → "rec-media.it" (keep as-is; readable enough)
    return domain


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------


def _get_hubspot_client():
    import hubspot

    token = os.getenv("HUBSPOT_ACCESS_TOKEN") or os.getenv("HUBSPOT_API_KEY")
    if not token:
        raise ValueError("Set HUBSPOT_ACCESS_TOKEN in your .env file")
    return hubspot.Client.create(access_token=token)


def _find_contact(client, email: str) -> Optional[object]:
    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": [
                    "email", "firstname", "lastname", "company", "hs_lead_source"
                ],
                "limit": 1,
            }
        )
        return result.results[0] if result.results else None
    except Exception as exc:
        log.error("HubSpot search error (%s): %s", email, exc)
        return None


def _create_contact(client, props: dict) -> Optional[str]:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    try:
        resp = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return resp.id
    except Exception as exc:
        log.error("HubSpot create error: %s", exc)
        return None


def _update_contact(client, contact_id: str, props: dict) -> bool:
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input={"properties": props},
        )
        return True
    except Exception as exc:
        log.error("HubSpot update error (%s): %s", contact_id, exc)
        return False


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------


def _sync_one(client, email: str, display_name: str) -> tuple[str, str]:
    """
    Create or update a HubSpot contact.
    Returns (status, contact_id) where status ∈ {Creato, Aggiornato, Ignorato}.
    """
    first, last = _split_name(display_name)
    company = _company_from_email(email)

    existing = _find_contact(client, email)

    if existing:
        contact_id: str = existing.id
        ex = existing.properties or {}
        updates: dict[str, str] = {}

        if not ex.get("firstname") and first:
            updates["firstname"] = first
        if not ex.get("lastname") and last:
            updates["lastname"] = last
        if not ex.get("company") and company:
            updates["company"] = company
        if ex.get("hs_lead_source") != "GMAIL":
            updates["hs_lead_source"] = "GMAIL"

        if updates:
            _update_contact(client, contact_id, updates)
            return "Aggiornato", contact_id
        return "Ignorato", contact_id

    # New contact
    props: dict[str, str] = {"email": email, "hs_lead_source": "GMAIL"}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company

    contact_id = _create_contact(client, props)
    return ("Creato", contact_id) if contact_id else ("Ignorato", "")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()).get("processed_threads", []))
        except Exception:
            pass
    return set()


def _save_state(processed: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"processed_threads": sorted(processed)}, indent=2)
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_once(gmail_service, hubspot_client, processed: set[str]) -> list[dict]:
    """Execute a single sync pass. Returns list of result records."""
    results: list[dict] = []

    for thread_meta in _fetch_inbox_threads(gmail_service):
        thread_id: str = thread_meta["id"]
        if thread_id in processed:
            continue

        thread = _get_thread(gmail_service, thread_id)
        if not thread:
            processed.add(thread_id)
            continue

        for message in thread.get("messages", []):
            subject = _get_header(message, "Subject")
            senders = _extract_senders(message)

            for email, name in senders:
                if not EMAIL_RE.fullmatch(email):
                    continue
                status, contact_id = _sync_one(hubspot_client, email, name)
                record = {
                    "thread_id": thread_id,
                    "subject": subject[:80],
                    "status": status,
                    "email": email,
                    "contact_id": contact_id,
                    "timestamp": datetime.utcnow().isoformat(timespec="seconds"),
                }
                results.append(record)
                log.info(
                    "[%s] %-45s  HubSpot ID: %-12s  %s",
                    status,
                    email,
                    contact_id or "—",
                    subject[:55],
                )

        processed.add(thread_id)

    _save_state(processed)
    return results


def print_summary(results: list[dict]) -> None:
    created = [r for r in results if r["status"] == "Creato"]
    updated = [r for r in results if r["status"] == "Aggiornato"]
    ignored = [r for r in results if r["status"] == "Ignorato"]
    log.info(
        "Ciclo: %d processati → Creati %d | Aggiornati %d | Ignorati %d",
        len(results),
        len(created),
        len(updated),
        len(ignored),
    )


def main() -> None:
    log.info("=== Gmail → HubSpot Sync avviato ===")
    log.info("Polling ogni %ds | Lookback %d giorni", POLL_INTERVAL, LOOKBACK_DAYS)

    gmail_service = _get_gmail_service()
    hubspot_client = _get_hubspot_client()
    processed = _load_state()

    log.info("Thread già processati in state: %d", len(processed))

    while True:
        try:
            results = run_once(gmail_service, hubspot_client, processed)
            print_summary(results)
        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
