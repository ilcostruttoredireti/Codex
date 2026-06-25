#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs senders as HubSpot contacts.
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput
from hubspot.crm.engagements import ApiException as EngApiException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".sync_state.json")

SKIP_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it", "tiscali.it"}
SKIP_EMAILS = set()  # Add your own email addresses here to ignore self-sent


def gmail_service():
    creds = None
    token_path = Path("token.json")
    creds_path = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def hubspot_client():
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN environment variable not set")
    return hubspot.Client.create(access_token=token)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_message_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def extract_real_sender(headers: list[dict]) -> tuple[str, str]:
    """Return (name, email) from message headers, handling Fw/Fwd patterns."""
    header_map = {h["name"].lower(): h["value"] for h in headers}
    from_raw = header_map.get("from", "")
    name, email = parseaddr(from_raw)

    # If the direct sender is an internal forwarder, try Reply-To
    reply_to = header_map.get("reply-to", "")
    if reply_to:
        rt_name, rt_email = parseaddr(reply_to)
        if rt_email and rt_email != email:
            return rt_name or name, rt_email

    return name, email


def extract_forwarded_sender(body_text: str) -> tuple[str, str] | None:
    """Parse 'Da: "Name" <email>' patterns from forwarded message bodies."""
    patterns = [
        r'Da:\s*"?([^"<\n]+?)"?\s*<([^>]+)>',
        r'From:\s*"?([^"<\n]+?)"?\s*<([^>]+)>',
        r'Da:\s*([^\s@<]+@[^\s>]+)',
        r'From:\s*([^\s@<]+@[^\s>]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, body_text, re.IGNORECASE | re.MULTILINE)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                return groups[0].strip(), groups[1].strip()
            elif len(groups) == 1:
                return "", groups[0].strip()
    return None


def decode_body(payload: dict) -> str:
    """Recursively extract plain text from a message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = decode_body(part)
            if text:
                return text
    return ""


def company_from_domain(domain: str) -> str | None:
    """Infer company name from an institutional email domain."""
    if domain in SKIP_DOMAINS:
        return None
    parts = domain.split(".")
    if len(parts) >= 2:
        name = parts[-2]
        return name.replace("-", " ").replace("_", " ").title()
    return None


def parse_contact(message: dict) -> dict | None:
    """Extract contact fields from a Gmail message resource."""
    headers = message.get("payload", {}).get("headers", [])
    name, email = extract_real_sender(headers)

    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    domain = email.split("@")[1]

    # Try to get a better sender from forwarded body
    if email in SKIP_EMAILS or domain in {"noreply", "no-reply", "mailer-daemon"}:
        body = decode_body(message.get("payload", {}))
        result = extract_forwarded_sender(body)
        if result:
            name, email = result
            email = email.lower().strip()
            domain = email.split("@")[1]
        else:
            return None

    if not name:
        name = email.split("@")[0].replace(".", " ").replace("_", " ").title()

    name_parts = name.strip().split(maxsplit=1)
    firstname = name_parts[0] if name_parts else ""
    lastname = name_parts[1] if len(name_parts) > 1 else ""

    company = company_from_domain(domain)

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def find_existing_contact(hs: hubspot.Client, email: str) -> dict | None:
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company"],
        limit=1,
    )
    try:
        resp = hs.crm.contacts.search_api.do_search(req)
        if resp.results:
            return resp.results[0]
    except ApiException as exc:
        log.warning("HubSpot search error for %s: %s", email, exc)
    return None


def create_contact(hs: hubspot.Client, contact: dict) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (status, hubspot_id)."""
    props = {
        "email": contact["email"],
        "firstname": contact["firstname"],
        "lastname": contact["lastname"],
    }
    if contact.get("company"):
        props["company"] = contact["company"]

    body = SimplePublicObjectInputForCreate(properties=props)
    try:
        result = hs.crm.contacts.basic_api.create(simple_public_object_input_for_create=body)
        _add_note(hs, result.id, contact["email"])
        log.info("CREATO  %s  → ID %s", contact["email"], result.id)
        return "Creato", result.id
    except ApiException as exc:
        log.error("Errore creazione %s: %s", contact["email"], exc)
        return "Errore", ""


def update_contact(hs: hubspot.Client, existing, contact: dict) -> tuple[str, str]:
    """Fill in missing fields on an existing contact. Returns (status, hubspot_id)."""
    current = existing.properties
    updates = {}

    if not current.get("firstname") and contact["firstname"]:
        updates["firstname"] = contact["firstname"]
    if not current.get("lastname") and contact["lastname"]:
        updates["lastname"] = contact["lastname"]
    if not current.get("company") and contact.get("company"):
        updates["company"] = contact["company"]

    if updates:
        body = SimplePublicObjectInput(properties=updates)
        try:
            hs.crm.contacts.basic_api.update(existing.id, simple_public_object_input=body)
            log.info("AGGIORNATO  %s  → ID %s  campi=%s", contact["email"], existing.id, list(updates))
            return "Aggiornato", existing.id
        except ApiException as exc:
            log.error("Errore aggiornamento %s: %s", contact["email"], exc)
            return "Errore", existing.id

    log.info("IGNORATO  %s  → ID %s", contact["email"], existing.id)
    return "Ignorato", existing.id


def _add_note(hs: hubspot.Client, contact_id: str, email: str):
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
    import time

    ts = str(int(time.time() * 1000))
    note_body = (
        f"Contatto acquisito automaticamente da Gmail (Inbound Gmail).\n"
        f"Email mittente: {email}\n"
        f"Data: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    props = {"hs_note_body": note_body, "hs_timestamp": ts}
    body = NoteCreate(properties=props, associations=[
        {
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        }
    ])
    try:
        hs.crm.objects.notes.basic_api.create(simple_public_object_input_for_create=body)
    except Exception as exc:
        log.debug("Nota non aggiunta per %s: %s", email, exc)


def fetch_new_messages(svc, last_history_id: str | None, max_results: int = 50) -> list[dict]:
    """Fetch unread inbox messages newer than last run."""
    if last_history_id:
        try:
            history = svc.users().history().list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            added = []
            for record in history.get("history", []):
                for msg in record.get("messagesAdded", []):
                    added.append(msg["message"]["id"])
            return added, history.get("historyId", last_history_id)
        except Exception:
            pass  # Fall through to list-based approach

    resp = svc.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
        q="is:unread newer_than:1d",
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    profile = svc.users().getProfile(userId="me").execute()
    return ids, profile.get("historyId", "")


def run():
    state = load_state()
    svc = gmail_service()
    hs = hubspot_client()

    message_ids, new_history_id = fetch_new_messages(svc, state["last_history_id"])
    processed = set(state.get("processed_message_ids", []))

    results = []
    for msg_id in message_ids:
        if msg_id in processed:
            continue

        try:
            msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
        except Exception as exc:
            log.warning("Cannot fetch message %s: %s", msg_id, exc)
            continue

        contact = parse_contact(msg)
        if not contact:
            processed.add(msg_id)
            continue

        existing = find_existing_contact(hs, contact["email"])
        if existing:
            status, hs_id = update_contact(hs, existing, contact)
        else:
            status, hs_id = create_contact(hs, contact)

        results.append({
            "stato": status,
            "email": contact["email"],
            "hs_id": hs_id,
        })
        processed.add(msg_id)

    state["last_history_id"] = new_history_id
    state["processed_message_ids"] = list(processed)[-500:]  # Keep last 500
    save_state(state)

    print("\n=== Riepilogo sincronizzazione Gmail → HubSpot ===")
    print(f"{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
    print("-" * 75)
    for r in results:
        print(f"{r['stato']:<12} {r['email']:<45} {r['hs_id']}")
    print(f"\nTotale processati: {len(results)}")

    return results


if __name__ == "__main__":
    run()
