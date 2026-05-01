#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs senders as HubSpot contacts.
"""

import os
import re
import time
import json
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
GMAIL_CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".sync_state.json"))

# Senders to ignore — automated / system accounts
IGNORED_SENDER_PATTERNS = re.compile(
    r"(no[-.]?reply|noreply|mailer-daemon|postmaster|notifications|do-not-reply"
    r"|donotreply|bounce|auto[-.]?reply|automatic)",
    re.IGNORECASE,
)

# Domains treated as generic (no company inferred)
GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                   "icloud.com", "libero.it", "virgilio.it", "tin.it", "tiscali.it"}

# ── Gmail helpers ────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate and return a Gmail API service client."""
    creds: Optional[Credentials] = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_header(headers: list[dict], name: str) -> str:
    """Extract a header value by name (case-insensitive)."""
    name_lower = name.lower()
    return next(
        (h["value"] for h in headers if h["name"].lower() == name_lower), ""
    )


def parse_sender(raw_from: str) -> tuple[str, str, str]:
    """
    Parse a From header into (email, firstname, lastname).
    Returns empty strings for unknown fields.
    """
    display_name, email = parseaddr(raw_from)
    email = email.strip().lower()
    display_name = display_name.strip()

    firstname = lastname = ""
    if display_name:
        parts = display_name.split()
        firstname = parts[0].capitalize() if parts else ""
        lastname = " ".join(parts[1:]).capitalize() if len(parts) > 1 else ""
    else:
        # Try to infer from local part: "beatrice.giongo" → Beatrice Giongo
        local = email.split("@")[0]
        name_parts = re.split(r"[._\-+]", local)
        if len(name_parts) >= 2 and all(p.isalpha() for p in name_parts):
            firstname = name_parts[0].capitalize()
            lastname = " ".join(name_parts[1:]).capitalize()

    return email, firstname, lastname


def domain_to_company(domain: str) -> str:
    """Heuristically convert a domain into a company display name."""
    if domain in GENERIC_DOMAINS:
        return ""
    # Strip TLD and ccTLD (e.g. marcheteatro.it → marcheteatro)
    parts = domain.split(".")
    # Remove common prefixes
    prefix_skip = {"www", "mail", "info", "galleria", "bn-na"}
    name_parts = [p for p in parts[:-1] if p not in prefix_skip]
    raw = name_parts[0] if name_parts else parts[0]
    # CamelCase or space-separate: marcheteatro → Marche Teatro (best-effort)
    return raw.replace("-", " ").replace("_", " ").title()


def fetch_new_messages(service, history_id: Optional[str] = None, max_results: int = 50) -> tuple[list[dict], str]:
    """
    Fetch new inbox messages since `history_id`.
    On first run (no history_id) fetches the latest `max_results` messages.
    Returns (messages, new_history_id).
    """
    if history_id:
        try:
            resp = service.users().history().list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            records = resp.get("history", [])
            msg_ids = [
                m["message"]["id"]
                for r in records
                for m in r.get("messagesAdded", [])
            ]
            new_history_id = resp.get("historyId", history_id)
            if not msg_ids:
                return [], new_history_id
            messages = []
            for mid in msg_ids:
                msg = service.users().messages().get(
                    userId="me", id=mid, format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                messages.append(msg)
            return messages, new_history_id
        except Exception as exc:
            log.warning("History fetch failed (%s), falling back to list", exc)

    # First run or fallback: fetch recent messages
    resp = service.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=max_results,
    ).execute()
    msg_ids = [m["id"] for m in resp.get("messages", [])]
    messages = []
    for mid in msg_ids:
        msg = service.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        messages.append(msg)
    new_history_id = messages[0].get("historyId") if messages else None
    return messages, new_history_id


# ── HubSpot helpers ──────────────────────────────────────────────────────────

def get_hubspot_client():
    if not HUBSPOT_ACCESS_TOKEN:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(client, email: str) -> Optional[dict]:
    """Return the HubSpot contact dict if the email exists, else None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[
                Filter(property_name="email", operator="EQ", value=email)
            ])
        ],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=search_req)
    if resp.results:
        return resp.results[0]
    return None


def build_contact_properties(email: str, firstname: str, lastname: str, company: str) -> dict:
    props = {
        "email": email,
        "hs_analytics_source": "EMAIL_MARKETING",  # closest standard value for Gmail
        "hs_lead_status": "NEW",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def create_contact(client, email: str, firstname: str, lastname: str, company: str) -> dict:
    props = build_contact_properties(email, firstname, lastname, company)
    contact_input = SimplePublicObjectInputForCreate(properties=props, associations=[])
    return client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=contact_input
    )


def update_contact(client, contact_id: str, existing: dict, firstname: str, lastname: str, company: str) -> bool:
    """Fill in only missing fields. Returns True if any update was applied."""
    existing_props = existing.properties
    updates = {}

    if firstname and not existing_props.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing_props.get("company"):
        updates["company"] = company
    if not existing_props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    if not updates:
        return False

    from hubspot.crm.contacts import SimplePublicObjectInput
    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return True


def add_activity_note(client, contact_id: str, email: str, subject: str) -> None:
    """Attach a NOTE activity to the contact recording the received email."""
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput
    from hubspot.crm.associations.v4.models import AssociationSpec, AssociationSpecAssociationCategory
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    note_body = (
        f"📧 Email ricevuta da: {email}\n"
        f"Oggetto: {subject or '(nessun oggetto)'}\n"
        f"Tag: Inbound Gmail\n"
        f"Data sync: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    note = client.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=NoteInput(
            properties={
                "hs_note_body": note_body,
                "hs_timestamp": str(timestamp_ms),
            }
        )
    )
    # Associate note → contact
    client.crm.associations.v4.basic_api.create(
        object_type="notes",
        object_id=note.id,
        to_object_type="contacts",
        to_object_id=contact_id,
        association_spec=[
            AssociationSpec(
                association_category=AssociationSpecAssociationCategory.HUBSPOT_DEFINED,
                association_type_id=202,
            )
        ],
    )


# ── State persistence ────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"history_id": None, "processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Core sync logic ──────────────────────────────────────────────────────────

def should_ignore(email: str) -> bool:
    local = email.split("@")[0]
    return bool(IGNORED_SENDER_PATTERNS.search(local))


def process_message(gmail_service, hubspot_client, msg: dict, processed_ids: set) -> dict:
    """
    Process a single Gmail message.
    Returns a result dict: {status, email, contact_id, subject}.
    """
    msg_id = msg["id"]
    if msg_id in processed_ids:
        return {"status": "IGNORATO", "reason": "già processato", "email": "", "contact_id": None}

    headers = msg.get("payload", {}).get("headers", [])
    raw_from = get_header(headers, "From")
    subject = get_header(headers, "Subject")

    if not raw_from:
        return {"status": "IGNORATO", "reason": "nessun mittente", "email": "", "contact_id": None}

    email, firstname, lastname = parse_sender(raw_from)
    if not email or "@" not in email:
        return {"status": "IGNORATO", "reason": "email non valida", "email": email, "contact_id": None}

    if should_ignore(email):
        return {"status": "IGNORATO", "reason": "mittente automatico", "email": email, "contact_id": None}

    domain = email.split("@")[1]
    company = domain_to_company(domain)

    existing = find_contact_by_email(hubspot_client, email)

    if existing:
        updated = update_contact(hubspot_client, existing.id, existing, firstname, lastname, company)
        try:
            add_activity_note(hubspot_client, existing.id, email, subject)
        except Exception as e:
            log.debug("Could not add note: %s", e)
        status = "AGGIORNATO" if updated else "IGNORATO"
        return {"status": status, "email": email, "contact_id": existing.id, "subject": subject}
    else:
        try:
            new_contact = create_contact(hubspot_client, email, firstname, lastname, company)
            try:
                add_activity_note(hubspot_client, new_contact.id, email, subject)
            except Exception as e:
                log.debug("Could not add note: %s", e)
            return {"status": "CREATO", "email": email, "contact_id": new_contact.id, "subject": subject}
        except ApiException as exc:
            if exc.status == 409:  # conflict — contact already exists
                existing_dup = find_contact_by_email(hubspot_client, email)
                if existing_dup:
                    update_contact(hubspot_client, existing_dup.id, existing_dup, firstname, lastname, company)
                    return {"status": "AGGIORNATO", "email": email, "contact_id": existing_dup.id, "subject": subject}
            raise


# ── Main loop ────────────────────────────────────────────────────────────────

def run_once(gmail_service, hubspot_client, state: dict) -> list[dict]:
    """Fetch new emails, sync contacts. Returns list of result dicts."""
    history_id = state.get("history_id")
    processed_ids = set(state.get("processed_message_ids", []))

    messages, new_history_id = fetch_new_messages(gmail_service, history_id)
    results = []

    for msg in messages:
        try:
            result = process_message(gmail_service, hubspot_client, msg, processed_ids)
            result["message_id"] = msg["id"]
            results.append(result)
            processed_ids.add(msg["id"])
            if result["status"] != "IGNORATO":
                log.info("[%s] %s → ID HubSpot: %s",
                         result["status"], result["email"], result["contact_id"])
        except Exception as exc:
            log.error("Errore processando msg %s: %s", msg.get("id"), exc)

    state["history_id"] = new_history_id
    # Keep only the last 1000 IDs to avoid unbounded growth
    state["processed_message_ids"] = list(processed_ids)[-1000:]
    save_state(state)
    return results


def print_report(results: list[dict]) -> None:
    """Print a formatted sync report."""
    created = [r for r in results if r["status"] == "CREATO"]
    updated = [r for r in results if r["status"] == "AGGIORNATO"]
    ignored = [r for r in results if r["status"] == "IGNORATO"]

    print("\n" + "═" * 60)
    print(f"  GMAIL → HUBSPOT SYNC  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("═" * 60)
    print(f"  Processati: {len(results)}  |  Creati: {len(created)}  |"
          f"  Aggiornati: {len(updated)}  |  Ignorati: {len(ignored)}")
    print("─" * 60)

    rows = [r for r in results if r["status"] != "IGNORATO"]
    if not rows:
        print("  Nessuna nuova variazione.")
    else:
        print(f"  {'STATO':<12} {'EMAIL':<42} {'ID HUBSPOT'}")
        print("  " + "─" * 58)
        for r in rows:
            print(f"  {r['status']:<12} {r['email']:<42} {r['contact_id']}")

    if ignored:
        print(f"\n  Ignorati ({len(ignored)}):")
        for r in ignored:
            reason = r.get("reason", "")
            email = r.get("email", "(no email)")
            print(f"    • {email}  [{reason}]")

    print("═" * 60 + "\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run a single pass then exit")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS,
                        help=f"Polling interval in seconds (default: {POLL_INTERVAL_SECONDS})")
    args = parser.parse_args()

    gmail = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()

    log.info("Avvio sync Gmail → HubSpot (intervallo: %ds)", args.interval)

    if args.once:
        results = run_once(gmail, hs, state)
        print_report(results)
        return

    while True:
        try:
            results = run_once(gmail, hs, state)
            if results:
                print_report(results)
            else:
                log.info("Nessuna nuova email.")
        except KeyboardInterrupt:
            log.info("Interruzione manuale.")
            break
        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc)

        log.info("Prossimo controllo tra %ds…", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
