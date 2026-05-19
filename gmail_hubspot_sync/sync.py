"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts senders, creates/updates HubSpot contacts.
"""

import os
import re
import time
import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min default
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")

# Domains to skip (internal / noise)
SKIP_DOMAINS = {"gmail.com", "googlemail.com"}
# Email addresses to never create as contacts
SKIP_EMAILS = set(os.getenv("SKIP_EMAILS", "").lower().split(","))


@dataclass
class ContactData:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source: str = "Gmail"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_FWD_PATTERN = re.compile(
    r'Da ["\']?([^"\'<\n]+?)["\']?\s+<([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)>',
    re.IGNORECASE,
)
_FWD_EMAIL_ONLY = re.compile(
    r'Da\s+([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)',
    re.IGNORECASE,
)


def _company_from_domain(domain: str) -> str:
    """Heuristic: turn a domain into a readable company name."""
    if domain in SKIP_DOMAINS:
        return ""
    parts = domain.split(".")
    name = parts[0] if parts else domain
    # Remove common prefixes
    for prefix in ("www", "mail", "info", "noreply"):
        if name.lower() == prefix and len(parts) > 1:
            name = parts[1]
    return name.replace("-", " ").replace("_", " ").title()


def _parse_name(local_part: str) -> tuple[str, str]:
    """Best-effort first/last name from the email local part."""
    clean = re.sub(r"[^a-zA-Z.]", "", local_part)
    parts = [p.capitalize() for p in clean.split(".") if p]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def contact_from_sender(sender_name: str, sender_email: str) -> Optional[ContactData]:
    sender_email = sender_email.strip().lower()
    if not sender_email or "@" not in sender_email:
        return None
    if sender_email in SKIP_EMAILS:
        return None

    local, domain = sender_email.split("@", 1)
    company = _company_from_domain(domain)

    # Parse name
    firstname, lastname = "", ""
    if sender_name:
        name_parts = sender_name.strip().split()
        if name_parts:
            firstname = name_parts[0].capitalize()
            lastname = " ".join(name_parts[1:]).title() if len(name_parts) > 1 else ""
    else:
        firstname, lastname = _parse_name(local)

    return ContactData(
        email=sender_email,
        firstname=firstname,
        lastname=lastname,
        company=company,
    )


def extract_contacts_from_message(msg: dict) -> list[ContactData]:
    """
    Extract contact(s) from a Gmail message dict.
    Handles forwarded messages by scanning the snippet/body for embedded senders.
    """
    contacts: list[ContactData] = []
    sender_header = msg.get("sender", "")
    snippet = msg.get("snippet", "")

    # --- Direct sender ---
    m = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>$', sender_header.strip())
    if m:
        name, email = m.group(1).strip(), m.group(2).strip()
    else:
        name, email = "", sender_header.strip()

    direct = contact_from_sender(name, email)
    if direct:
        contacts.append(direct)

    # --- Forwarded / embedded senders in snippet ---
    for match in _FWD_PATTERN.finditer(snippet):
        emb_name, emb_email = match.group(1).strip(), match.group(2).strip()
        c = contact_from_sender(emb_name, emb_email)
        if c and c.email != (direct.email if direct else ""):
            contacts.append(c)

    for match in _FWD_EMAIL_ONLY.finditer(snippet):
        emb_email = match.group(1).strip().lower()
        if direct and emb_email == direct.email:
            continue
        if any(c.email == emb_email for c in contacts):
            continue
        c = contact_from_sender("", emb_email)
        if c:
            contacts.append(c)

    return contacts


# ---------------------------------------------------------------------------
# State management (tracks last processed historyId to avoid duplicates)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, state: dict) -> list[dict]:
    """Return messages newer than the last processed historyId."""
    last_history_id = state.get("last_history_id")
    messages = []

    if last_history_id:
        try:
            history = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=last_history_id,
                    labelId="INBOX",
                    historyTypes=["messageAdded"],
                )
                .execute()
            )
            for record in history.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_stub = added["message"]
                    # Fetch minimal headers
                    full = (
                        service.users()
                        .messages()
                        .get(userId="me", id=msg_stub["id"], format="metadata",
                             metadataHeaders=["From", "Subject"])
                        .execute()
                    )
                    headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
                    messages.append({
                        "id": full["id"],
                        "sender": headers.get("From", ""),
                        "subject": headers.get("Subject", ""),
                        "snippet": full.get("snippet", ""),
                        "labelIds": full.get("labelIds", []),
                    })
            # Update historyId
            new_hid = history.get("historyId")
            if new_hid:
                state["last_history_id"] = new_hid
        except Exception as exc:
            log.warning("History fetch failed (%s), falling back to inbox scan", exc)
            last_history_id = None

    if not last_history_id:
        # First run: list inbox, grab historyId anchor
        result = service.users().messages().list(userId="me", labelIds=["INBOX"], maxResults=50).execute()
        profile = service.users().getProfile(userId="me").execute()
        state["last_history_id"] = profile.get("historyId")
        for stub in result.get("messages", []):
            full = (
                service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="metadata",
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
            messages.append({
                "id": full["id"],
                "sender": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "snippet": full.get("snippet", ""),
                "labelIds": full.get("labelIds", []),
            })

    return messages


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def get_hs_client() -> hubspot.HubSpot:
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN env var not set")
    return hubspot.HubSpot(access_token=HUBSPOT_TOKEN)


def find_contact_by_email(hs: hubspot.HubSpot, email: str) -> Optional[dict]:
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.results:
        return resp.results[0]
    return None


def upsert_contact(hs: hubspot.HubSpot, contact: ContactData) -> tuple[str, str]:
    """
    Returns (status, hubspot_id) where status is 'created'|'updated'|'skipped'.
    """
    existing = find_contact_by_email(hs, contact.email)

    props_to_set: dict[str, str] = {}

    # Always set source tag if missing
    def _want(current_val, new_val):
        return new_val and not current_val

    if existing:
        ex_props = existing.properties
        if _want(ex_props.get("firstname"), contact.firstname):
            props_to_set["firstname"] = contact.firstname
        if _want(ex_props.get("lastname"), contact.lastname):
            props_to_set["lastname"] = contact.lastname
        if _want(ex_props.get("company"), contact.company):
            props_to_set["company"] = contact.company
        if _want(ex_props.get("hs_lead_source"), contact.source):
            props_to_set["hs_lead_source"] = contact.source

        if not props_to_set:
            return "skipped", str(existing.id)

        hs.crm.contacts.basic_api.update(
            contact_id=str(existing.id),
            simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
                properties=props_to_set
            ),
        )
        return "updated", str(existing.id)

    # Create new contact
    create_props = {
        "email": contact.email,
        "hs_lead_source": contact.source,
    }
    if contact.firstname:
        create_props["firstname"] = contact.firstname
    if contact.lastname:
        create_props["lastname"] = contact.lastname
    if contact.company:
        create_props["company"] = contact.company

    created = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=create_props
        )
    )
    return "created", str(created.id)


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def process_messages(messages: list[dict], hs: hubspot.HubSpot, state: dict) -> None:
    processed_emails: set[str] = set(state.get("processed_emails_session", []))
    results = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in state.get("processed_msg_ids", []):
            continue

        contacts = extract_contacts_from_message(msg)
        for contact in contacts:
            if contact.email in processed_emails:
                continue
            processed_emails.add(contact.email)

            try:
                status, hs_id = upsert_contact(hs, contact)
            except ApiException as exc:
                log.error("HubSpot API error for %s: %s", contact.email, exc)
                status, hs_id = "error", "—"

            icon = {"created": "✅", "updated": "🔄", "skipped": "⏭️", "error": "❌"}.get(status, "?")
            log.info("%s %-10s | %-45s | HS ID: %s", icon, status.upper(), contact.email, hs_id)
            results.append({"status": status, "email": contact.email, "hs_id": hs_id})

        state.setdefault("processed_msg_ids", []).append(msg_id)

    state["processed_emails_session"] = list(processed_emails)

    # Print summary table
    if results:
        print("\n" + "─" * 70)
        print(f"{'STATO':<12} {'EMAIL':<45} {'HS ID'}")
        print("─" * 70)
        for r in results:
            print(f"{r['status']:<12} {r['email']:<45} {r['hs_id']}")
        print("─" * 70 + "\n")


def run() -> None:
    log.info("Avvio Gmail → HubSpot sync (polling ogni %ds)", POLL_INTERVAL_SECONDS)
    state = load_state()
    gmail = get_gmail_service()
    hs = get_hs_client()

    while True:
        try:
            log.info("Controllo nuove email…")
            messages = fetch_new_messages(gmail, state)
            log.info("Trovati %d messaggi da processare", len(messages))
            process_messages(messages, hs, state)
            save_state(state)
        except Exception as exc:
            log.error("Errore ciclo sync: %s", exc, exc_info=True)

        log.info("Prossimo controllo tra %ds", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
