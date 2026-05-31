#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Polls Gmail inbox for new inbound emails, extracts sender data, then
creates or updates the corresponding HubSpot contact. Runs continuously
until interrupted (Ctrl-C) or once when --once is passed.

Usage:
    python gmail_hubspot_sync.py          # continuous polling
    python gmail_hubspot_sync.py --once   # single pass, then exit
    python gmail_hubspot_sync.py --dry-run  # print actions without writing to HubSpot
    python gmail_hubspot_sync.py --setup  # run Gmail OAuth flow and exit
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── logging ──────────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── config ────────────────────────────────────────────────────────────────────

HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
CREDENTIALS_FILE = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path(os.getenv("GOOGLE_TOKEN_FILE", "token.json"))
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MAX_MESSAGES = int(os.getenv("MAX_MESSAGES_PER_CYCLE", "50"))

SKIP_DOMAINS: set[str] = {
    d.strip().lower()
    for d in os.getenv(
        "SKIP_DOMAINS",
        "noreply.com,no-reply.com,mailer-daemon.com,bounce.com,"
        "notifications.com,updates.com,donotreply.com",
    ).split(",")
    if d.strip()
}

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class ContactData:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    message_id: str = ""
    received_at: str = ""  # ISO-8601


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: str = ""
    reason: str = ""


# ── state persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State file corrupted; starting fresh.")
    return {"last_checked_ts": 0, "processed_message_ids": []}


def save_state(state: dict) -> None:
    # Keep processed_message_ids capped to last 5000 entries to avoid unbounded growth
    state["processed_message_ids"] = state["processed_message_ids"][-5000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail client ──────────────────────────────────────────────────────────────

def build_gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                log.error(
                    "credentials.json not found. Download OAuth 2.0 client credentials "
                    "from Google Cloud Console → APIs & Services → Credentials and save "
                    "as '%s'.",
                    CREDENTIALS_FILE,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
        log.info("Gmail token saved to %s", TOKEN_FILE)

    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, since_ts: int, max_results: int) -> list[dict]:
    """Return raw Gmail message dicts for inbox messages newer than since_ts."""
    query_parts = ["in:inbox"]
    if since_ts > 0:
        # Gmail `after:` accepts Unix epoch seconds
        query_parts.append(f"after:{since_ts}")
    query = " ".join(query_parts)

    try:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
    except Exception as exc:
        log.error("Gmail list error: %s", exc)
        return []

    message_refs = resp.get("messages", [])
    messages = []
    for ref in message_refs:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="metadata",
                     metadataHeaders=["From", "Date", "Message-ID"])
                .execute()
            )
            messages.append(msg)
        except Exception as exc:
            log.warning("Could not fetch message %s: %s", ref["id"], exc)
    return messages


# ── contact extraction ────────────────────────────────────────────────────────

_NAME_EMAIL_RE = re.compile(r"^(.*?)\s*<([^>]+)>$")


def parse_from_header(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From: header."""
    name, email = parseaddr(from_header)
    return name.strip(), email.strip().lower()


def split_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' → ('First', 'Last'). Handles single names."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def domain_from_email(email: str) -> str:
    """Return the domain part of an email address."""
    return email.split("@", 1)[-1] if "@" in email else ""


def company_from_domain(domain: str) -> str:
    """Best-effort company name: strip TLD and capitalize."""
    parts = domain.rsplit(".", 2)
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return domain.capitalize()


def header_value(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def extract_contact(message: dict) -> Optional[ContactData]:
    """Build a ContactData from a Gmail message dict, or None if it should be skipped."""
    headers = message.get("payload", {}).get("headers", [])
    from_raw = header_value(headers, "From")
    date_raw = header_value(headers, "Date")

    if not from_raw:
        return None

    display_name, email = parse_from_header(from_raw)

    if not email or "@" not in email:
        return None

    domain = domain_from_email(email)

    if domain in SKIP_DOMAINS:
        log.debug("Skipping no-reply domain: %s", domain)
        return None

    # Skip automated senders heuristically
    local_part = email.split("@")[0]
    if any(kw in local_part for kw in ("noreply", "no-reply", "donotreply", "mailer", "bounce")):
        return None

    firstname, lastname = split_name(display_name)
    company = company_from_domain(domain)

    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_raw)
        received_at = dt.astimezone(timezone.utc).isoformat()
    except Exception:
        received_at = datetime.now(timezone.utc).isoformat()

    return ContactData(
        email=email,
        firstname=firstname,
        lastname=lastname,
        company=company,
        message_id=message.get("id", ""),
        received_at=received_at,
    )


# ── HubSpot client ────────────────────────────────────────────────────────────

def build_hubspot_client():
    import hubspot

    if not HUBSPOT_TOKEN:
        log.error(
            "HUBSPOT_ACCESS_TOKEN not set. Create a Private App in HubSpot and add "
            "the token to your .env file."
        )
        sys.exit(1)
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact_by_email(client, email: str) -> Optional[dict]:
    """Return the first matching HubSpot contact dict or None."""
    from hubspot.crm.contacts import PublicObjectSearchRequest

    search_req = PublicObjectSearchRequest(
        filter_groups=[
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        if result.total > 0:
            return result.results[0]
    except Exception as exc:
        log.error("HubSpot search error for %s: %s", email, exc)
    return None


def create_hubspot_contact(client, data: ContactData) -> Optional[str]:
    """Create a new HubSpot contact. Returns the contact ID or None on error."""
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    props = {
        "email": data.email,
        "hs_lead_source": "Gmail",
    }
    if data.firstname:
        props["firstname"] = data.firstname
    if data.lastname:
        props["lastname"] = data.lastname
    if data.company:
        props["company"] = data.company

    try:
        contact = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return contact.id
    except Exception as exc:
        log.error("HubSpot create error for %s: %s", data.email, exc)
        return None


def update_hubspot_contact(
    client, contact_id: str, existing: dict, data: ContactData
) -> bool:
    """Fill only missing fields on an existing contact. Returns True on success."""
    from hubspot.crm.contacts import SimplePublicObjectInput

    existing_props = existing.properties if hasattr(existing, "properties") else {}
    updates: dict[str, str] = {}

    def missing(key: str) -> bool:
        return not existing_props.get(key)

    if missing("firstname") and data.firstname:
        updates["firstname"] = data.firstname
    if missing("lastname") and data.lastname:
        updates["lastname"] = data.lastname
    if missing("company") and data.company:
        updates["company"] = data.company
    if missing("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if not updates:
        return True  # nothing to change

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except Exception as exc:
        log.error("HubSpot update error for %s: %s", data.email, exc)
        return False


def add_note_to_contact(client, contact_id: str, data: ContactData) -> None:
    """Attach an inbound-email note to the contact's timeline."""
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate

    ts_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body = (
        f"📥 Email inbound ricevuta da {data.email}"
        f"{' (' + data.firstname + ' ' + data.lastname + ')' if data.firstname else ''}"
        f"\nData ricezione: {data.received_at}"
        f"\nFonte: Gmail — tag: Inbound Gmail"
    )
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(
                properties={"hs_note_body": body, "hs_timestamp": ts_ms},
                associations=[
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
            )
        )
        log.debug("Note added (id=%s) for contact %s", note.id, contact_id)
    except Exception as exc:
        log.warning("Could not add note for contact %s: %s", contact_id, exc)


# ── sync logic ────────────────────────────────────────────────────────────────

def sync_contact(
    client, data: ContactData, dry_run: bool = False
) -> SyncResult:
    existing = find_contact_by_email(client, data.email)

    if existing:
        contact_id = existing.id
        if dry_run:
            log.info("[DRY-RUN] Aggiornato  %s  (id=%s)", data.email, contact_id)
            return SyncResult("Aggiornato", data.email, contact_id)
        ok = update_hubspot_contact(client, contact_id, existing, data)
        if ok:
            add_note_to_contact(client, contact_id, data)
            return SyncResult("Aggiornato", data.email, contact_id)
        return SyncResult("Ignorato", data.email, contact_id, reason="update failed")
    else:
        if dry_run:
            log.info("[DRY-RUN] Creato     %s", data.email)
            return SyncResult("Creato", data.email, "DRY_RUN_ID")
        contact_id = create_hubspot_contact(client, data)
        if contact_id:
            add_note_to_contact(client, contact_id, data)
            return SyncResult("Creato", data.email, contact_id)
        return SyncResult("Ignorato", data.email, reason="create failed")


def print_result(r: SyncResult) -> None:
    icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}.get(r.status, "❓")
    extra = f"  ({r.reason})" if r.reason else ""
    print(
        f"{icon}  {r.status:<12}  {r.email:<45}  HubSpot ID: {r.hubspot_id or '—'}{extra}"
    )


# ── main loop ─────────────────────────────────────────────────────────────────

def run_cycle(
    gmail_svc,
    hubspot_client,
    state: dict,
    dry_run: bool,
) -> list[SyncResult]:
    since_ts = state.get("last_checked_ts", 0)
    processed_ids: set[str] = set(state.get("processed_message_ids", []))

    messages = fetch_new_messages(gmail_svc, since_ts, MAX_MESSAGES)
    log.info("Trovati %d messaggi da analizzare (after ts=%d)", len(messages), since_ts)

    results: list[SyncResult] = []
    new_processed: list[str] = []
    now_ts = int(datetime.now(timezone.utc).timestamp())

    for msg in messages:
        msg_id = msg.get("id", "")
        if msg_id in processed_ids:
            continue

        contact_data = extract_contact(msg)
        new_processed.append(msg_id)

        if contact_data is None:
            results.append(SyncResult("Ignorato", "(no sender)", reason="filtered"))
            continue

        result = sync_contact(hubspot_client, contact_data, dry_run=dry_run)
        results.append(result)
        print_result(result)

    # Persist state
    state["last_checked_ts"] = now_ts
    state["processed_message_ids"] = list(processed_ids) + new_processed
    save_state(state)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing to HubSpot")
    parser.add_argument("--setup", action="store_true", help="Run Gmail OAuth flow only and exit")
    args = parser.parse_args()

    print("=" * 65)
    print("  Gmail → HubSpot Contact Sync")
    print("=" * 65)

    gmail_svc = build_gmail_service()

    if args.setup:
        print("✅ Gmail OAuth completato. Ora puoi eseguire il sync.")
        return

    hubspot_client = build_hubspot_client()
    state = load_state()

    if args.dry_run:
        log.info("Modalità DRY-RUN attiva – nessuna scrittura su HubSpot.")

    if args.once:
        run_cycle(gmail_svc, hubspot_client, state, dry_run=args.dry_run)
        return

    log.info("Avvio polling continuo ogni %d secondi. Ctrl-C per fermare.", POLL_INTERVAL)
    while True:
        try:
            run_cycle(gmail_svc, hubspot_client, state, dry_run=args.dry_run)
        except KeyboardInterrupt:
            log.info("Interruzione manuale. Stato salvato.")
            break
        except Exception as exc:
            log.exception("Errore nel ciclo: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
