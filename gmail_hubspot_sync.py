#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls Gmail inbox continuously and syncs new sender contacts to HubSpot.

Setup:
  1. Copy .env.example to .env and fill in credentials.
  2. pip install -r requirements.txt
  3. python gmail_hubspot_sync.py

For Gmail OAuth:
  - Create a Google Cloud project, enable Gmail API.
  - Create OAuth 2.0 credentials (Desktop app).
  - Run the one-time auth flow to obtain a refresh token:
      python -c "from gmail_hubspot_sync import run_oauth_flow; run_oauth_flow()"
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────

GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN", "")
GMAIL_TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")

STATE_FILE = os.getenv("STATE_FILE", "state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "300"))        # seconds between polls
GMAIL_QUERY_EXTRA = os.getenv("GMAIL_QUERY_EXTRA", "")        # extra Gmail search filter

# Sender addresses to never sync (internal relay accounts, self)
IGNORE_SENDERS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv("IGNORE_SENDERS", "").split(",")
    if e.strip()
)

# Patterns that identify system / automated senders → skip them
SKIP_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"mailer-daemon@",
        r"(no-?reply|noreply|donotreply)@",
        r"postmaster@",
        r"bounce[+\-@]",
        r"posta-certificata@",
        r"^delivery(status)?@",
        r"@(googlemail|google)\.com$",
        r"@.*\.pec(\.|$)",              # certified-mail relay domains
        r"@app\.mailvox\.it$",          # mailing-list relay
        r"@.*\.legalmail\.it$",
    ]
]

# Generic / role display-names that don't map to a real person
GENERIC_FIRST_NAMES: frozenset[str] = frozenset({
    "ufficio stampa", "press office", "redazione", "comunicazione",
    "segreteria", "info", "admin", "administration",
})

# Personal e-mail provider domains → no company extraction
PERSONAL_DOMAINS: frozenset[str] = frozenset({
    "gmail", "yahoo", "hotmail", "outlook", "libero", "virgilio",
    "alice", "tiscali", "icloud", "live", "msn", "me",
})

# Italian forwarded-message header patterns (Da / From)
_FW_PATTERNS: list[re.Pattern] = [
    # Da "Display Name" email@domain
    re.compile(
        r'Da\s+"([^"]+)"\s+<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
        re.IGNORECASE,
    ),
    # Da: "Display Name" <email>
    re.compile(
        r'Da:\s+"?([^"<\n]+?)"?\s+<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
        re.IGNORECASE,
    ),
    # Da email@domain (no display name)
    re.compile(
        r'Da\s+<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
        re.IGNORECASE,
    ),
    # English: From: "Name" <email>
    re.compile(
        r'From:\s+"?([^"<\n]+?)"?\s+<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
        re.IGNORECASE,
    ),
    # From: email (bare)
    re.compile(
        r'From:\s+<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
        re.IGNORECASE,
    ),
]


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def get_gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri=GMAIL_TOKEN_URI,
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=GMAIL_SCOPES,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def run_oauth_flow() -> None:
    """One-time helper: run interactive OAuth flow and print the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not GMAIL_CLIENT_ID or not GMAIL_CLIENT_SECRET:
        sys.exit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")
    client_config = {
        "installed": {
            "client_id": GMAIL_CLIENT_ID,
            "client_secret": GMAIL_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": GMAIL_TOKEN_URI,
            "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)
    print(f"\nGMAIL_REFRESH_TOKEN={creds.refresh_token}\n")
    print("Add this line to your .env file.")


def fetch_inbox_message_ids(service, after_timestamp: Optional[int] = None) -> list[str]:
    """Return message IDs from inbox, optionally filtered to after a Unix timestamp."""
    query_parts = ["in:inbox -from:me"]
    if after_timestamp:
        # Gmail 'after:' accepts epoch seconds
        query_parts.append(f"after:{after_timestamp}")
    if GMAIL_QUERY_EXTRA:
        query_parts.append(GMAIL_QUERY_EXTRA)
    query = " ".join(query_parts)

    ids: list[str] = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=500, pageToken=page_token)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def get_message_headers(service, msg_id: str) -> dict:
    """Fetch only From / Subject / Date headers (lightweight)."""
    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )


def get_message_plain_body(service, msg_id: str) -> str:
    """Fetch the full message and extract plain-text body."""
    msg = (
        service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    )
    return _extract_text(msg.get("payload", {}))


def _extract_text(payload: dict) -> str:
    data = payload.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        mime = part.get("mimeType", "")
        if mime in ("text/plain", "multipart/mixed", "multipart/alternative", "multipart/related"):
            text = _extract_text(part)
            if text:
                return text
    return ""


def header_value(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# ── Sender extraction ──────────────────────────────────────────────────────────

def parse_from_header(from_header: str) -> tuple[str, str]:
    """Parse 'Display Name <email>' → (display_name, email). Lowercase email."""
    # "Name" <email> or Name <email>
    m = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_header.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    # bare email
    m = re.match(r'^([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})$', from_header.strip())
    if m:
        return "", m.group(1).lower()
    # email anywhere in string
    m = re.search(r'([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', from_header)
    if m:
        name = re.sub(r'<[^>]+>', '', from_header).strip().strip('"').strip()
        return name, m.group(1).lower()
    return "", ""


def extract_forwarded_sender(body: str) -> tuple[str, str]:
    """
    Scan first 3 KB of a forwarded email body for the original sender.
    Returns (display_name, email_addr) or ("", "") if not found.
    """
    snippet = body[:3000]
    for pattern in _FW_PATTERNS:
        m = pattern.search(snippet)
        if not m:
            continue
        groups = m.groups()
        if len(groups) == 2:
            a, b = groups[0].strip(), groups[1].strip().lower()
            if "@" in a:           # first capture is actually the email
                return "", a.lower()
            return a, b
        elif len(groups) == 1:
            candidate = groups[0].strip()
            if "@" in candidate:
                return "", candidate.lower()
    return "", ""


def should_skip(email_addr: str) -> bool:
    """Return True for system / bounce / relay addresses."""
    if email_addr.lower() in IGNORE_SENDERS:
        return True
    return any(p.search(email_addr) for p in SKIP_PATTERNS)


# ── Contact data helpers ───────────────────────────────────────────────────────

def parse_name(display_name: str) -> tuple[str, str]:
    """'First Last' → ('First', 'Last'). Multi-word last name supported."""
    cleaned = display_name.strip().strip('"').strip()
    parts = cleaned.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0].capitalize(), ""
    return parts[0].capitalize(), " ".join(p.capitalize() for p in parts[1:])


def company_from_domain(email_addr: str) -> str:
    """
    Best-effort company name from the email domain.
    Returns '' for personal providers and generic subdomains.
    """
    domain = email_addr.split("@")[-1].lower()
    parts = domain.split(".")
    # strip common subdomains
    while parts and parts[0] in {"www", "mail", "smtp", "mx", "m", "app", "sender", "info"}:
        parts = parts[1:]
    if not parts:
        return ""
    if parts[0] in PERSONAL_DOMAINS:
        return ""
    # Remove TLDs to get the brand name
    brand = parts[0].replace("-", " ")
    return brand.title()


def build_properties(display_name: str, email_addr: str) -> dict:
    firstname, lastname = parse_name(display_name) if display_name else ("", "")
    company = company_from_domain(email_addr)
    props: dict[str, str] = {
        "email": email_addr,
        "lifecyclestage": "lead",
    }
    if firstname and firstname.lower() not in GENERIC_FIRST_NAMES:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


# ── HubSpot helpers ────────────────────────────────────────────────────────────

def get_hubspot_client():
    import hubspot as hs_sdk
    return hs_sdk.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact(hs_client, email_addr: str) -> Optional[dict]:
    """Return the first HubSpot contact matching email_addr, or None."""
    try:
        result = hs_client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "email",
                                "operator": "EQ",
                                "value": email_addr,
                            }
                        ]
                    }
                ],
                "properties": ["email", "firstname", "lastname", "company", "lifecyclestage"],
                "limit": 1,
            }
        )
        return result.results[0] if result.results else None
    except Exception as exc:
        log.warning("HubSpot search error for %s: %s", email_addr, exc)
        return None


def add_inbound_note(hs_client, contact_id: str) -> None:
    """Attach an 'Inbound Gmail' note engagement to the contact."""
    try:
        hs_client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create={
                "properties": {
                    "hs_note_body": "Fonte contatto: Inbound Gmail",
                    "hs_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            }
        )
    except Exception as exc:
        log.debug("Note skipped for contact %s: %s", contact_id, exc)


def sync_contact(hs_client, display_name: str, email_addr: str) -> tuple[str, str]:
    """
    Ensure the contact exists in HubSpot and is up to date.

    Returns:
      (status, contact_id)  where status ∈ {'CREATO', 'AGGIORNATO', 'IGNORATO'}
    """
    existing = find_contact(hs_client, email_addr)
    desired = build_properties(display_name, email_addr)

    if existing:
        contact_id = existing.id
        current = existing.properties or {}
        # Only push fields that are currently blank
        updates = {k: v for k, v in desired.items() if v and not current.get(k)}
        if updates:
            try:
                hs_client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input={"properties": updates},
                )
                return "AGGIORNATO", contact_id
            except Exception as exc:
                log.error("Update failed for %s: %s", email_addr, exc)
        return "IGNORATO", contact_id

    # New contact
    try:
        created = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create={"properties": desired}
        )
        contact_id = created.id
        add_inbound_note(hs_client, contact_id)
        return "CREATO", contact_id
    except Exception as exc:
        log.error("Create failed for %s: %s", email_addr, exc)
        return "IGNORATO", ""


# ── Core processing loop ───────────────────────────────────────────────────────

# Forwarding relay addresses whose bodies should be parsed for the real sender
_RELAY_ADDRS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv("RELAY_SENDERS", "").split(",")
    if e.strip()
)


def process_batch(gmail_service, hs_client, state: dict) -> dict:
    """
    Fetch new messages from Gmail inbox and sync each unique sender to HubSpot.
    Returns updated state dict.
    """
    last_ts: Optional[int] = state.get("last_processed_timestamp")
    msg_ids = fetch_inbox_message_ids(gmail_service, after_timestamp=last_ts)

    if not msg_ids:
        log.info("Nessun nuovo messaggio.")
        return state

    log.info("%d messaggi trovati.", len(msg_ids))

    new_max_ts = last_ts or 0
    seen: set[str] = set()

    for msg_id in msg_ids:
        try:
            msg = get_message_headers(gmail_service, msg_id)
        except Exception as exc:
            log.warning("Impossibile leggere messaggio %s: %s", msg_id, exc)
            continue

        # Track the latest processed timestamp so we can resume from here
        ts = int(msg.get("internalDate", 0)) // 1000
        new_max_ts = max(new_max_ts, ts)

        from_raw = header_value(msg, "From")
        display_name, email_addr = parse_from_header(from_raw)

        # If this message came via a forwarding relay, look up the real sender in the body
        if email_addr in _RELAY_ADDRS:
            try:
                body = get_message_plain_body(gmail_service, msg_id)
                fw_name, fw_email = extract_forwarded_sender(body)
                if fw_email:
                    display_name, email_addr = fw_name, fw_email
            except Exception as exc:
                log.debug("Body parse failed for msg %s: %s", msg_id, exc)

        if not email_addr:
            continue
        if should_skip(email_addr):
            log.debug("Skip (sistema): %s", email_addr)
            continue
        if email_addr in seen:
            continue
        seen.add(email_addr)

        status, contact_id = sync_contact(hs_client, display_name, email_addr)

        row = f"  Stato: {status:<12} | Email: {email_addr:<45} | ID HubSpot: {contact_id or 'N/A'}"
        print(row)
        log.info("[%s] %s → %s", status, email_addr, contact_id or "N/A")

    state["last_processed_timestamp"] = new_max_ts
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    return state


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    missing = [v for v in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "HUBSPOT_TOKEN") if not os.getenv(v)]
    if missing:
        sys.exit(f"Variabili mancanti nel .env: {', '.join(missing)}\nVedi .env.example")

    log.info("Gmail → HubSpot Sync avviato (polling ogni %ds).", POLL_INTERVAL)
    gmail = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()

    while True:
        log.info("── Scansione in corso ────────────────────────────")
        try:
            state = process_batch(gmail, hs, state)
            save_state(state)
        except Exception as exc:
            log.error("Errore nella scansione: %s", exc, exc_info=True)

        log.info("Prossima scansione tra %ds.", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
