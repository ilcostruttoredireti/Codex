#!/usr/bin/env python3
"""
gmail_hubspot_sync.py

Monitors the Gmail inbox and syncs senders as contacts in HubSpot.
Avoids duplicates using email as unique key, updates existing records
with missing fields, and creates new contacts for unknown senders.

Usage:
    python gmail_hubspot_sync.py             # continuous polling (default 60s)
    python gmail_hubspot_sync.py --once      # single run then exit
    python gmail_hubspot_sync.py --interval 120  # poll every 2 minutes

Environment variables required:
    HUBSPOT_ACCESS_TOKEN   HubSpot private-app token with contacts scope
    GMAIL_CREDENTIALS_FILE Path to OAuth2 credentials JSON (default: credentials.json)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Google Gmail API ──────────────────────────────────────────────────────────
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    sys.exit(
        "Missing Google libraries. Run:\n"
        "  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
    )

# ── HubSpot SDK ───────────────────────────────────────────────────────────────
try:
    import hubspot
    from hubspot.crm.contacts import (
        ApiException,
        SimplePublicObjectInputForCreate,
    )
    from hubspot.crm.contacts.models import (
        Filter,
        FilterGroup,
        PublicObjectSearchRequest,
        SimplePublicObjectInput,
    )
    from hubspot.crm.objects.models import (
        SimplePublicObjectInputForCreate as NoteInputForCreate,
    )
except ImportError:
    sys.exit(
        "Missing HubSpot library. Run:\n"
        "  pip install hubspot-api-client"
    )

# ─────────────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path("token.json")
STATE_FILE = Path(".gmail_sync_state.json")

SKIP_PREFIXES = {
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "postmaster", "bounce", "notifications", "admin",
    "support", "info-", "automatic",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_sender(raw: str) -> dict:
    """
    Parse 'Display Name <email@domain.com>' or bare 'email@domain.com'
    into a structured dict with email, firstname, lastname, company, domain.
    """
    email_match = re.search(r"[\w.+\-]+@[\w.\-]+\.\w+", raw)
    if not email_match:
        return {}
    email = email_match.group(0).lower().strip()

    name_match = re.match(r'^"?([^"<]+?)"?\s*<', raw)
    full_name = name_match.group(1).strip() if name_match else ""

    parts = full_name.split() if full_name else []
    firstname = parts[0] if parts else ""
    lastname = " ".join(parts[1:]) if len(parts) > 1 else ""

    domain = email.split("@")[1] if "@" in email else ""
    # Best-effort company from domain: strip common TLDs, capitalise
    company_raw = domain.split(".")[0] if domain else ""
    company = company_raw.replace("-", " ").title() if company_raw else ""

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
    }


def is_automated(email: str) -> bool:
    """Return True for automated/system senders that should not be synced."""
    local = email.split("@")[0].lower()
    return any(local.startswith(p) or local == p.rstrip("-") for p in SKIP_PREFIXES)


def extract_name_from_snippet(snippet: str) -> tuple[str, str]:
    """
    Heuristic: look for 'Saluti X Y' / 'Cordiali saluti X Y' patterns
    in Italian email snippets to extract a name.
    """
    patterns = [
        r"saluti\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"Cordiali\s+saluti\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, snippet)
        if m:
            parts = m.group(1).split()
            return parts[0], " ".join(parts[1:])
    return "", ""


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service(credentials_file: str):
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(credentials_file).exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {credentials_file}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_my_email(svc) -> str:
    return svc.users().getProfile(userId="me").execute().get("emailAddress", "")


def fetch_inbox_threads(svc, max_results: int = 50) -> list[dict]:
    try:
        resp = svc.users().threads().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=max_results,
        ).execute()
        return resp.get("threads", [])
    except HttpError as e:
        log.error(f"Gmail API error fetching threads: {e}")
        return []


def get_inbound_messages(svc, thread_id: str, my_email: str) -> list[dict]:
    """
    Return messages in the thread that were sent TO us (inbound),
    not sent BY us.
    """
    try:
        thread = svc.users().threads().get(
            userId="me", threadId=thread_id, format="metadata"
        ).execute()
    except HttpError as e:
        log.error(f"Gmail API error reading thread {thread_id}: {e}")
        return []

    inbound = []
    for msg in thread.get("messages", []):
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        from_addr = headers.get("from", "")
        to_addr = headers.get("to", "") + headers.get("cc", "")

        # Skip messages sent by us
        if my_email.lower() in from_addr.lower():
            continue

        # Only process if we're in to/cc (true inbound)
        if my_email.lower() not in to_addr.lower():
            continue

        inbound.append({
            "msg_id": msg["id"],
            "from": from_addr,
            "subject": headers.get("subject", ""),
            "snippet": msg.get("snippet", ""),
            "date": headers.get("date", ""),
        })

    return inbound


# ── HubSpot ───────────────────────────────────────────────────────────────────

def find_contact(hs_client, email: str) -> Optional[dict]:
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        resp = hs_client.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        return resp.results[0].to_dict() if resp.results else None
    except ApiException as e:
        log.error(f"HubSpot search error for {email}: {e}")
        return None


def create_contact(hs_client, data: dict) -> Optional[str]:
    props = {
        "email": data["email"],
        "leadsource": "EMAIL_MARKETING",
        "hs_analytics_source_data_1": "Inbound Gmail",
    }
    if data.get("firstname"):
        props["firstname"] = data["firstname"]
    if data.get("lastname"):
        props["lastname"] = data["lastname"]
    if data.get("company"):
        props["company"] = data["company"]

    try:
        result = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except ApiException as e:
        log.error(f"HubSpot create error for {data['email']}: {e}")
        return None


def update_contact(hs_client, contact_id: str, data: dict, existing: dict) -> bool:
    """Fill only missing fields on the existing contact."""
    existing_props = existing.get("properties", {})
    updates = {}
    for field in ("firstname", "lastname", "company"):
        if data.get(field) and not existing_props.get(field):
            updates[field] = data[field]

    if not updates:
        return False

    try:
        hs_client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as e:
        log.error(f"HubSpot update error for contact {contact_id}: {e}")
        return False


def add_email_activity(hs_client, contact_id: str, subject: str, date_str: str) -> None:
    """Associate an inbound-email note on the contact's timeline."""
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    try:
        hs_client.crm.objects.basic_api.create(
            object_type="notes",
            simple_public_object_input_for_create=NoteInputForCreate(
                properties={
                    "hs_note_body": f"📥 Email in arrivo da Gmail\nOggetto: {subject}\nData: {date_str}\nTag: Inbound Gmail",
                    "hs_timestamp": timestamp_ms,
                },
                associations=[
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
            ),
        )
    except Exception as e:
        log.warning(f"Could not add note to contact {contact_id}: {e}")


# ── Main sync logic ───────────────────────────────────────────────────────────

def process_message(
    hs_client,
    msg: dict,
    state: dict,
    add_notes: bool = True,
) -> dict:
    """Process a single inbound message: find/create/update HubSpot contact."""
    msg_id = msg["msg_id"]
    if msg_id in state["processed_message_ids"]:
        return {"stato": "Ignorato (già elaborato)", "email": "—", "hubspot_id": "—"}

    sender = parse_sender(msg["from"])
    if not sender:
        state["processed_message_ids"].append(msg_id)
        return {"stato": "Ignorato (mittente non valido)", "email": msg["from"], "hubspot_id": "—"}

    email = sender["email"]

    if is_automated(email):
        state["processed_message_ids"].append(msg_id)
        return {"stato": "Ignorato (automatico)", "email": email, "hubspot_id": "—"}

    # Try to enrich name from snippet if From header had none
    if not sender["firstname"]:
        fn, ln = extract_name_from_snippet(msg.get("snippet", ""))
        if fn:
            sender["firstname"] = fn
        if ln:
            sender["lastname"] = ln

    existing = find_contact(hs_client, email)

    if existing:
        contact_id = existing["id"]
        updated = update_contact(hs_client, contact_id, sender, existing)
        stato = "Aggiornato" if updated else "Ignorato (già completo)"
    else:
        contact_id = create_contact(hs_client, sender)
        stato = "Creato" if contact_id else "Errore creazione"

    if contact_id and add_notes:
        add_email_activity(hs_client, contact_id, msg["subject"], msg["date"])

    state["processed_message_ids"].append(msg_id)
    # Keep state bounded to last 5000 message IDs
    state["processed_message_ids"] = state["processed_message_ids"][-5000:]

    return {
        "stato": stato,
        "email": email,
        "hubspot_id": contact_id or (existing["id"] if existing else "—"),
    }


def run_sync(svc, hs_client, state: dict, add_notes: bool = True) -> list[dict]:
    my_email = get_my_email(svc)
    log.info(f"Mailbox: {my_email}")

    threads = fetch_inbox_threads(svc)
    log.info(f"Thread da esaminare: {len(threads)}")

    results = []
    for thread in threads:
        messages = get_inbound_messages(svc, thread["id"], my_email)
        for msg in messages:
            result = process_message(hs_client, msg, state, add_notes=add_notes)
            if result["stato"] not in ("Ignorato (già elaborato)",):
                results.append(result)
                log.info(
                    f"[{result['stato']}] {result['email']} → HubSpot ID: {result['hubspot_id']}"
                )

    save_state(state)
    return results


def print_table(results: list[dict]) -> None:
    if not results:
        print("\nNessun contatto da elaborare.\n")
        return
    w_stato = max(len("Stato"), max(len(r["stato"]) for r in results))
    w_email = max(len("Email"), max(len(r["email"]) for r in results))
    w_id = max(len("HubSpot ID"), max(len(str(r["hubspot_id"])) for r in results))
    sep = f"+{'-'*(w_stato+2)}+{'-'*(w_email+2)}+{'-'*(w_id+2)}+"
    header = f"| {'Stato':<{w_stato}} | {'Email':<{w_email}} | {'HubSpot ID':<{w_id}} |"
    print(f"\n{sep}\n{header}\n{sep}")
    for r in results:
        print(f"| {r['stato']:<{w_stato}} | {r['email']:<{w_email}} | {str(r['hubspot_id']):<{w_id}} |")
    print(f"{sep}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    parser.add_argument("--no-notes", action="store_true", help="Skip adding timeline notes to contacts")
    args = parser.parse_args()

    hubspot_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        sys.exit("Error: HUBSPOT_ACCESS_TOKEN environment variable not set.")

    credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")

    hs_client = hubspot.Client.create(access_token=hubspot_token)
    svc = get_gmail_service(credentials_file)
    state = load_state()

    add_notes = not args.no_notes

    if args.once:
        results = run_sync(svc, hs_client, state, add_notes=add_notes)
        print_table(results)
        return

    log.info(f"Avvio monitoraggio Gmail → HubSpot (intervallo: {args.interval}s)")
    while True:
        try:
            results = run_sync(svc, hs_client, state, add_notes=add_notes)
            if results:
                print_table(results)
        except KeyboardInterrupt:
            log.info("Interrotto dall'utente.")
            break
        except Exception as e:
            log.error(f"Errore nel ciclo: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
