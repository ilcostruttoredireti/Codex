#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously and syncs sender contacts to HubSpot.
"""

import os
import re
import json
import time
import logging
import pickle
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
]

STATE_FILE = Path("processed_messages.json")
TOKEN_FILE = Path("token.pickle")
CREDS_FILE = Path("credentials.json")
LOG_FILE = Path("gmail_hubspot_sync.log")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL", "60"))
GMAIL_QUERY = os.environ.get("GMAIL_QUERY", "in:inbox -from:me newer_than:1d")

# Domains/patterns that are system senders (not real contacts)
SKIP_DOMAINS = frozenset(
    {
        "facebookmail.com",
        "notifications.google.com",
        "linkedin.com",
        "twitter.com",
        "accounts.google.com",
        "mail.instagram.com",
    }
)

SKIP_EMAIL_PATTERNS = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "mailer-daemon",
        "bounce",
        "automated",
        "notification@",
        "alerts@",
        "newsletter@",
        "unsubscribe@",
    }
)


# ── Auth helpers ───────────────────────────────────────────────────────────────


def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as fh:
            creds = pickle.load(fh)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                raise FileNotFoundError(
                    "credentials.json not found. "
                    "Download it from Google Cloud Console (OAuth 2.0 Desktop app)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as fh:
            pickle.dump(creds, fh)

    return build("gmail", "v1", credentials=creds)


def get_hubspot_client():
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable is required.")
    return hubspot.Client.create(access_token=token)


# ── Contact parsing helpers ────────────────────────────────────────────────────


def parse_name(display_name: str) -> tuple[str | None, str | None]:
    if not display_name:
        return None, None
    parts = display_name.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if parts:
        return parts[0], None
    return None, None


def domain_to_company(domain: str) -> str:
    """Best-effort company name from email domain."""
    parts = domain.split(".")
    # Strip common hosting subdomains
    if len(parts) > 2 and parts[0] in ("mail", "email", "info", "news", "press"):
        parts = parts[1:]
    name = parts[0] if parts else domain
    return name.replace("-", " ").replace("_", " ").title()


def should_skip(email: str) -> bool:
    email_lower = email.lower()
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""

    if domain in SKIP_DOMAINS:
        return True
    for pat in SKIP_EMAIL_PATTERNS:
        if pat in email_lower:
            return True

    my_email = os.environ.get("MY_EMAIL", "").lower()
    if my_email and email_lower == my_email:
        return True

    return False


def extract_forwarded_senders(text: str) -> list[tuple[str, str]]:
    """Parse original sender lines from forwarded-email bodies."""
    senders: list[tuple[str, str]] = []
    patterns = [
        r'(?:Da|From):\s+"?([^"<\n]+)"?\s+<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
        r'(?:Da|From):\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    ]
    for pat in patterns:
        for match in re.findall(pat, text, re.IGNORECASE):
            if isinstance(match, tuple) and len(match) == 2:
                senders.append((match[1].strip(), match[0].strip()))
            else:
                senders.append((str(match).strip(), ""))
    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for email, name in senders:
        if email not in seen:
            seen.add(email)
            result.append((email, name))
    return result


# ── HubSpot helpers ────────────────────────────────────────────────────────────


def hs_find_contact(hs: hubspot.Client, email: str) -> tuple[str | None, dict]:
    """Return (contact_id, properties_dict) or (None, {})."""
    try:
        resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
                ],
                properties=["email", "firstname", "lastname", "company", "leadsource"],
                limit=1,
            )
        )
        if resp.results:
            c = resp.results[0]
            return c.id, (c.properties or {})
    except ApiException as exc:
        logging.error("HubSpot search failed for %s: %s", email, exc)
    return None, {}


def hs_create_contact(
    hs: hubspot.Client,
    email: str,
    firstname: str | None,
    lastname: str | None,
    company: str | None,
) -> str | None:
    props: dict[str, str] = {
        "email": email,
        "leadsource": "Gmail",
        "hs_lead_status": "NEW",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    try:
        resp = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        return resp.id
    except ApiException as exc:
        logging.error("HubSpot create failed for %s: %s", email, exc)
        return None


def hs_update_contact(
    hs: hubspot.Client,
    contact_id: str,
    existing: dict,
    firstname: str | None,
    lastname: str | None,
    company: str | None,
) -> bool:
    updates: dict[str, str] = {}
    if firstname and not existing.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing.get("company"):
        updates["company"] = company
    if not existing.get("leadsource"):
        updates["leadsource"] = "Gmail"

    if not updates:
        return False

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        logging.error("HubSpot update failed for contact %s: %s", contact_id, exc)
        return False


def hs_add_email_activity(
    hs: hubspot.Client,
    contact_id: str,
    subject: str,
    date_str: str,
) -> None:
    """Log the received email as a note associated to the contact."""
    try:
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteInput,
        )

        note = NoteInput(
            properties={
                "hs_note_body": (
                    f"Email ricevuta via Gmail\n"
                    f"Oggetto: {subject}\n"
                    f"Data: {date_str}\n"
                    f"Tag: Inbound Gmail"
                ),
                "hs_timestamp": str(int(datetime.now().timestamp() * 1000)),
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
        )
        hs.crm.objects.notes.basic_api.create(simple_public_object_input_for_create=note)
    except Exception as exc:
        logging.warning("Could not add note to contact %s: %s", contact_id, exc)


# ── Core processing ────────────────────────────────────────────────────────────


def process_sender(
    hs: hubspot.Client,
    email: str,
    display_name: str,
    subject: str,
    date_str: str,
) -> dict:
    """Sync one sender to HubSpot. Returns result dict."""
    if should_skip(email):
        return {"status": "Ignorato", "email": email, "contact_id": None}

    domain = email.split("@")[-1] if "@" in email else ""
    firstname, lastname = parse_name(display_name)
    company = domain_to_company(domain) if domain else None

    contact_id, existing = hs_find_contact(hs, email)

    if contact_id:
        changed = hs_update_contact(hs, contact_id, existing, firstname, lastname, company)
        status = "Aggiornato" if changed else "Invariato"
    else:
        contact_id = hs_create_contact(hs, email, firstname, lastname, company)
        status = "Creato" if contact_id else "Errore"

    if contact_id:
        hs_add_email_activity(hs, contact_id, subject, date_str)

    return {"status": status, "email": email, "contact_id": contact_id}


def get_message_metadata(gmail, msg_id: str) -> dict:
    msg = (
        gmail.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From", "Subject", "Date"])
        .execute()
    )
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers


# ── State persistence ──────────────────────────────────────────────────────────


def load_state() -> set[str]:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("processed", []))
        except (json.JSONDecodeError, KeyError):
            pass
    return set()


def save_state(ids: set[str]) -> None:
    STATE_FILE.write_text(
        json.dumps({"processed": list(ids), "updated": datetime.now().isoformat()}, indent=2)
    )


# ── Main loop ──────────────────────────────────────────────────────────────────


def run_once(gmail, hs: hubspot.Client, processed: set[str]) -> tuple[int, int, int, int]:
    """Process one batch. Returns (new, created, updated, ignored)."""
    try:
        result = (
            gmail.users()
            .threads()
            .list(userId="me", q=GMAIL_QUERY, maxResults=50)
            .execute()
        )
    except HttpError as exc:
        logging.error("Gmail list error: %s", exc)
        return 0, 0, 0, 0

    threads = result.get("threads", [])
    new_total = created = updated = ignored = 0

    for thread in threads:
        try:
            thread_data = (
                gmail.users()
                .threads()
                .get(userId="me", id=thread["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
        except HttpError as exc:
            logging.warning("Could not fetch thread %s: %s", thread["id"], exc)
            continue

        for msg in thread_data.get("messages", []):
            msg_id = msg["id"]
            if msg_id in processed:
                continue

            new_total += 1
            headers = {
                h["name"].lower(): h["value"]
                for h in msg.get("payload", {}).get("headers", [])
            }
            from_raw = headers.get("from", "")
            subject = headers.get("subject", "(no subject)")
            date_str = headers.get("date", "")

            display_name, sender_email = parseaddr(from_raw)
            if not sender_email:
                processed.add(msg_id)
                ignored += 1
                continue

            res = process_sender(hs, sender_email, display_name, subject, date_str)
            processed.add(msg_id)

            status = res["status"]
            logging.info(
                "[%s] %-40s  contact_id=%s", status, res["email"], res.get("contact_id") or "-"
            )

            # Print to stdout in a clear tabular format
            print(f"  {status:<12} | {res['email']:<45} | {res.get('contact_id') or '-'}")

            if status == "Creato":
                created += 1
            elif status == "Aggiornato":
                updated += 1
            else:
                ignored += 1

    return new_total, created, updated, ignored


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(LOG_FILE)),
        ],
    )

    logging.info("=== Gmail → HubSpot Sync avviato ===")
    logging.info("Query Gmail: %s", GMAIL_QUERY)
    logging.info("Polling ogni %d secondi", POLL_INTERVAL_SECONDS)

    gmail = get_gmail_service()
    hs = get_hubspot_client()
    processed = load_state()
    logging.info("ID messaggi già elaborati: %d", len(processed))

    print(f"\n{'Stato':<12} | {'Email contatto':<45} | {'HubSpot ID'}")
    print("-" * 75)

    try:
        while True:
            new, created, updated, ignored = run_once(gmail, hs, processed)

            if new:
                logging.info(
                    "Batch: %d nuovi → %d creati, %d aggiornati, %d ignorati",
                    new, created, updated, ignored,
                )
                save_state(processed)
            else:
                logging.debug("Nessun nuovo messaggio.")

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logging.info("Interruzione manuale. Salvo lo stato...")
        save_state(processed)
        logging.info("Uscita.")


if __name__ == "__main__":
    main()
