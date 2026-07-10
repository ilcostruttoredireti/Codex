#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for incoming emails, extracts sender data,
and creates or updates contacts in HubSpot.
Uses email address as unique deduplication key.
"""

import os
import json
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup
from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("sync_state.json")

AUTOMATED_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notifications", "notify", "mailer-daemon", "postmaster",
    "bounce", "bounces", "newsletter", "unsubscribe",
    "ads-noreply", "pinbot", "notify-noreply",
)


def is_automated_sender(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(local.startswith(prefix) for prefix in AUTOMATED_PREFIXES)


def parse_sender(raw_sender: str) -> tuple[str, str, str]:
    """Return (email, first_name, last_name) from a raw From header."""
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', raw_sender.strip())
    if match:
        display_name = match.group(1).strip()
        email = match.group(2).strip().lower()
        parts = display_name.split(None, 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
    else:
        email = raw_sender.strip().lower()
        first, last = "", ""
    return email, first, last


def domain_to_company(email: str) -> str:
    """Derive a human-readable company name from the email domain."""
    domain = email.split("@")[-1]
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def load_gmail_credentials() -> Credentials:
    token_path = Path("token.json")
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_threads": [], "last_run": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_new_inbox_threads(service, last_history_id: Optional[str]) -> list[dict]:
    """Return threads not yet seen, using history API when possible."""
    threads = []
    query = "in:inbox -from:me"

    if last_history_id:
        try:
            resp = service.users().history().list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            history = resp.get("history", [])
            seen_thread_ids = set()
            for record in history:
                for msg in record.get("messagesAdded", []):
                    tid = msg["message"].get("threadId")
                    if tid and tid not in seen_thread_ids:
                        seen_thread_ids.add(tid)
                        threads.append({"id": tid})
            return threads
        except Exception as exc:
            logger.warning("History API failed (%s), falling back to search.", exc)

    result = service.users().threads().list(
        userId="me", q=query, maxResults=50
    ).execute()
    return result.get("threads", [])


def get_thread_sender(service, thread_id: str) -> Optional[tuple[str, str, str]]:
    """Return (email, first_name, last_name) for the first message in a thread."""
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From"],
    ).execute()
    messages = thread.get("messages", [])
    if not messages:
        return None
    headers = {h["name"]: h["value"] for h in messages[0].get("payload", {}).get("headers", [])}
    raw_from = headers.get("From", "")
    if not raw_from:
        return None
    return parse_sender(raw_from)


def find_hubspot_contact(hs_client, email: str) -> Optional[dict]:
    search_req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[
            Filter(property_name="email", operator="EQ", value=email)
        ])],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    results = hs_client.crm.contacts.search_api.do_search(search_req)
    if results.total > 0:
        return results.results[0]
    return None


def create_or_update_contact(hs_client, email: str, first: str, last: str) -> tuple[str, str]:
    """
    Return (status, contact_id).
    status is 'Creato', 'Aggiornato', or 'Ignorato'.
    """
    company = domain_to_company(email)
    existing = find_hubspot_contact(hs_client, email)

    properties = {
        "email": email,
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if first:
        properties["firstname"] = first
    if last:
        properties["lastname"] = last
    if company:
        properties.setdefault("company", company)

    try:
        if existing:
            contact_id = existing.id
            # Only patch fields that are missing in HubSpot
            updates = {}
            existing_props = existing.properties or {}
            for key, val in properties.items():
                if not existing_props.get(key) and val:
                    updates[key] = val
            if updates:
                hs_client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input={"properties": updates},
                )
                return "Aggiornato", contact_id
            return "Ignorato", contact_id
        else:
            resp = hs_client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=properties
                )
            )
            contact_id = resp.id
            _create_inbound_note(hs_client, contact_id, email)
            return "Creato", contact_id
    except ApiException as exc:
        logger.error("HubSpot API error for %s: %s", email, exc)
        return "Ignorato", "ERROR"


def _create_inbound_note(hs_client, contact_id: str, email: str) -> None:
    """Attach a note to the contact marking it as an Inbound Gmail contact."""
    try:
        timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        note = hs_client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(
                properties={
                    "hs_note_body": f"Contatto acquisito via Gmail Inbound.\nEmail: {email}\nTag: Inbound Gmail",
                    "hs_timestamp": timestamp_ms,
                }
            )
        )
        hs_client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:
        logger.warning("Could not create note for %s: %s", email, exc)


def run_sync():
    hubspot_api_key = os.environ.get("HUBSPOT_API_KEY")
    if not hubspot_api_key:
        raise RuntimeError("HUBSPOT_API_KEY environment variable not set.")

    hs_client = hubspot.Client.create(access_token=hubspot_api_key)
    gmail_creds = load_gmail_credentials()
    gmail_service = build("gmail", "v1", credentials=gmail_creds)

    state = load_state()
    threads = fetch_new_inbox_threads(gmail_service, state.get("last_history_id"))

    profile = gmail_service.users().getProfile(userId="me").execute()
    new_history_id = profile.get("historyId")

    processed = set(state.get("processed_threads", []))
    results = []

    for thread in threads:
        tid = thread["id"]
        if tid in processed:
            continue

        sender = get_thread_sender(gmail_service, tid)
        if not sender:
            continue

        email, first, last = sender
        if is_automated_sender(email):
            logger.debug("Skipping automated sender: %s", email)
            processed.add(tid)
            continue

        status, contact_id = create_or_update_contact(hs_client, email, first, last)
        processed.add(tid)

        results.append({
            "stato": status,
            "email": email,
            "hubspot_id": contact_id,
        })

        logger.info("[%s] %s → HubSpot ID: %s", status, email, contact_id)

    state["last_history_id"] = new_history_id
    state["processed_threads"] = list(processed)[-500:]  # keep last 500 to bound file size
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    print("\n=== Sync completata ===")
    print(f"{'Stato':<12} {'Email':<40} {'HubSpot ID'}")
    print("-" * 70)
    for r in results:
        print(f"{r['stato']:<12} {r['email']:<40} {r['hubspot_id']}")
    if not results:
        print("Nessun nuovo contatto da processare.")

    return results


if __name__ == "__main__":
    run_sync()
