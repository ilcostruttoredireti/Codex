"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts sender info, creates/updates HubSpot contacts.
"""

import os
import re
import time
import json
import base64
import logging
from email.utils import parseaddr, parsedate_to_datetime
from datetime import datetime, timezone

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "gmail_credentials.json")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

# Domains to skip (own/internal addresses, mailing lists, etc.)
SKIP_DOMAINS = {
    d.strip()
    for d in os.getenv("SKIP_DOMAINS", "gmail.com,googlemail.com").split(",")
    if d.strip()
}

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── State helpers (track last processed history ID) ───────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def build_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GMAIL_CREDENTIALS_FILE):
                raise FileNotFoundError(
                    f"Missing {GMAIL_CREDENTIALS_FILE}. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_initial_history_id(service) -> str:
    """Return the current historyId so we only process future messages."""
    profile = service.users().getProfile(userId="me").execute()
    return profile["historyId"]


def fetch_new_messages(service, start_history_id: str) -> tuple[list[dict], str]:
    """
    Use Gmail History API to get messages added since start_history_id.
    Returns (list_of_message_stubs, new_history_id).
    """
    messages = []
    new_history_id = start_history_id
    page_token = None

    while True:
        kwargs = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded"],
        }
        if page_token:
            kwargs["pageToken"] = page_token

        try:
            result = service.users().history().list(**kwargs).execute()
        except HttpError as e:
            if e.resp.status == 404:
                # historyId expired; caller should reset
                raise
            raise

        new_history_id = result.get("historyId", new_history_id)

        for record in result.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                # Only INBOX messages
                if "INBOX" in msg.get("labelIds", []):
                    messages.append(msg)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return messages, new_history_id


def get_message_details(service, message_id: str) -> dict | None:
    """Fetch full message metadata."""
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "Date", "Subject"],
        ).execute()
        return msg
    except HttpError:
        return None


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Parse 'From' header → (full_name, email, first_name, last_name).
    Returns (full_name, first_name, last_name, email).
    """
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    first_name = ""
    last_name = ""
    if display_name:
        parts = display_name.strip().split()
        first_name = parts[0] if parts else ""
        last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    return display_name, first_name, last_name, email


def extract_company_from_domain(email: str) -> str:
    """Derive a company name from the email domain."""
    if "@" not in email:
        return ""
    domain = email.split("@")[1]
    # Strip common TLDs / subdomains for a readable name
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return domain


def get_header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# ── HubSpot helpers ────────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def hs_search_contact_by_email(email: str) -> dict | None:
    """Return existing HubSpot contact or None."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    resp = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts"
    resp = requests.post(url, json={"properties": props}, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, json={"properties": props}, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def hs_create_email_activity(contact_id: str, subject: str, from_email: str, received_at: str) -> None:
    """Log an inbound email as a HubSpot Engagement (email activity)."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/emails"
    payload = {
        "properties": {
            "hs_timestamp": received_at,
            "hs_email_direction": "INCOMING_EMAIL",
            "hs_email_status": "RECEIVED",
            "hs_email_subject": subject or "(no subject)",
            "hs_email_sender_email": from_email,
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 198,  # email → contact
                    }
                ],
            }
        ],
    }
    try:
        resp = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
        resp.raise_for_status()
    except requests.HTTPError as e:
        log.warning("Could not create email activity for %s: %s", contact_id, e)


# ── Core sync logic ────────────────────────────────────────────────────────────

def build_contact_properties(
    first_name: str,
    last_name: str,
    email: str,
    company: str,
) -> dict:
    props: dict = {
        "email": email,
        "hs_lead_source": CONTACT_SOURCE,
    }
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if company:
        props["company"] = company
    return props


def sync_contact(
    email: str,
    first_name: str,
    last_name: str,
    company: str,
    subject: str,
    received_at: str,
) -> tuple[str, str]:
    """
    Create or update a HubSpot contact.
    Returns (status, contact_id) where status ∈ {Creato, Aggiornato, Ignorato}.
    """
    existing = hs_search_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        current = existing.get("properties", {})

        update_props: dict = {}
        if first_name and not current.get("firstname"):
            update_props["firstname"] = first_name
        if last_name and not current.get("lastname"):
            update_props["lastname"] = last_name
        if company and not current.get("company"):
            update_props["company"] = company

        if update_props:
            hs_update_contact(contact_id, update_props)
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        props = build_contact_properties(first_name, last_name, email, company)
        created = hs_create_contact(props)
        contact_id = created["id"]
        status = "Creato"

    # Always log the inbound email as a timeline activity
    hs_create_email_activity(contact_id, subject, email, received_at)

    return status, contact_id


def process_message(service, msg_stub: dict) -> dict | None:
    """
    Process a single Gmail message stub → sync contact.
    Returns a result dict or None if skipped.
    """
    msg = get_message_details(service, msg_stub["id"])
    if not msg:
        return None

    from_header = get_header(msg, "From")
    subject = get_header(msg, "Subject")
    date_header = get_header(msg, "Date")

    if not from_header:
        return None

    display_name, first_name, last_name, email = parse_sender(from_header)

    if not email or "@" not in email:
        return None

    domain = email.split("@")[1]
    if domain in SKIP_DOMAINS:
        log.debug("Skipping %s (domain in skip list)", email)
        return None

    # Parse received timestamp (fallback to now)
    try:
        received_dt = parsedate_to_datetime(date_header)
        received_at = str(int(received_dt.timestamp() * 1000))
    except Exception:
        received_at = str(int(datetime.now(timezone.utc).timestamp() * 1000))

    company = extract_company_from_domain(email)

    try:
        status, contact_id = sync_contact(
            email=email,
            first_name=first_name,
            last_name=last_name,
            company=company,
            subject=subject,
            received_at=received_at,
        )
    except requests.HTTPError as e:
        log.error("HubSpot error for %s: %s — %s", email, e, e.response.text if e.response else "")
        return None

    result = {
        "stato": status,
        "email_contatto": email,
        "id_contatto_hubspot": contact_id,
        "nome": display_name or email,
        "azienda": company,
        "oggetto": subject,
    }
    return result


# ── Main loop ──────────────────────────────────────────────────────────────────

def run() -> None:
    if not HUBSPOT_API_KEY:
        raise EnvironmentError("HUBSPOT_API_KEY is not set.")

    log.info("Building Gmail service...")
    service = build_gmail_service()

    state = load_state()
    history_id = state.get("history_id")

    if not history_id:
        log.info("No saved state — starting from current inbox position.")
        history_id = get_initial_history_id(service)
        save_state({"history_id": history_id})
        log.info("Initial historyId saved: %s. Waiting for new emails...", history_id)

    log.info("Polling for new emails every %d seconds. Press Ctrl+C to stop.", POLL_INTERVAL_SECONDS)

    while True:
        try:
            messages, new_history_id = fetch_new_messages(service, history_id)

            if messages:
                log.info("Found %d new inbox message(s).", len(messages))

            for stub in messages:
                result = process_message(service, stub)
                if result:
                    log.info(
                        "[%s] %s | ID HubSpot: %s | Azienda: %s | Oggetto: %s",
                        result["stato"],
                        result["email_contatto"],
                        result["id_contatto_hubspot"],
                        result["azienda"],
                        result["oggetto"],
                    )
                    print(
                        f"\n{'─'*60}\n"
                        f"  Stato            : {result['stato']}\n"
                        f"  Email contatto   : {result['email_contatto']}\n"
                        f"  ID HubSpot       : {result['id_contatto_hubspot']}\n"
                        f"  Nome             : {result['nome']}\n"
                        f"  Azienda          : {result['azienda']}\n"
                        f"  Oggetto email    : {result['oggetto']}\n"
                    )

            if new_history_id != history_id:
                history_id = new_history_id
                save_state({"history_id": history_id})

        except HttpError as e:
            if e.resp.status == 404:
                log.warning("historyId expired — resetting to current position.")
                history_id = get_initial_history_id(service)
                save_state({"history_id": history_id})
            else:
                log.error("Gmail API error: %s", e)
        except requests.RequestException as e:
            log.error("Network error: %s", e)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
