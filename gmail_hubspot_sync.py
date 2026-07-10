"""
Gmail → HubSpot Contact Sync
Reads inbound Gmail emails, extracts sender info, creates or updates HubSpot contacts.

Dependencies:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client hubspot-api-client python-dotenv

Required environment variables (see .env.example):
    GMAIL_CREDENTIALS_FILE  - path to Google OAuth2 credentials JSON
    GMAIL_TOKEN_FILE        - path to cached OAuth2 token JSON (auto-created on first run)
    HUBSPOT_ACCESS_TOKEN    - HubSpot private app token
    GMAIL_QUERY             - optional Gmail search query (default: "in:inbox newer_than:1d -from:me")
    GMAIL_MAX_RESULTS       - max emails per run (default: 50)
    STATE_FILE              - path to JSON file storing last-processed message IDs (default: .sync_state.json)
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox newer_than:1d -from:me")
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "50"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".sync_state.json"))

# Sender prefixes/patterns that are automated and should be skipped
SKIP_PREFIXES = (
    "no-reply",
    "noreply",
    "notify-",
    "notifications@",
    "notification@",
    "mailer@",
    "bounce@",
    "postmaster@",
    "daemon@",
    "donotreply",
    "do-not-reply",
    "autoresponder@",
    "newsletters@",
    "newsletter@",
    "unsubscribe@",
    "ads-",
    "pinbot@",
    "alerts@",
    "automated@",
    "system@",
)

SKIP_DOMAINS = {
    "accounts.google.com",
    "mail.google.com",
    "notifications.google.com",
    "youtube.com",
    "facebook.com",
    "twitter.com",
    "linkedin.com",
    "instagram.com",
    "discord.com",
    "slack.com",
}

# HubSpot tag added to every contact synced from Gmail
GMAIL_SOURCE_TAG = "Inbound Gmail"


# ---------------------------------------------------------------------------
# State helpers (track processed message IDs to avoid re-processing)
# ---------------------------------------------------------------------------

def load_state() -> set:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            return set(data.get("processed_ids", []))
        except (json.JSONDecodeError, KeyError):
            return set()
    return set()


def save_state(processed_ids: set) -> None:
    STATE_FILE.write_text(json.dumps({"processed_ids": list(processed_ids)}, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = None

    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, scopes)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, scopes)
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_messages(service) -> list[dict]:
    """Return a flat list of message metadata dicts from the inbox."""
    messages = []
    request = (
        service.users()
        .messages()
        .list(userId="me", q=GMAIL_QUERY, maxResults=GMAIL_MAX_RESULTS)
    )
    while request is not None:
        response = request.execute()
        messages.extend(response.get("messages", []))
        request = service.users().messages().list_next(request, response)
    return messages


def get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Parse the From header into (display_name, email, domain).
    Example: 'John Doe <john@example.com>' -> ('John Doe', 'john@example.com', 'example.com')
    """
    match = re.match(r"^(?:\"?(.+?)\"?\s+)?<([^>]+)>$", from_header.strip())
    if match:
        name = (match.group(1) or "").strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        # bare email address
        name = ""
        email = from_header.strip().lower()

    domain = email.split("@")[-1] if "@" in email else ""
    return name, email, domain


def is_automated(email: str, domain: str) -> bool:
    local = email.split("@")[0].lower()
    if domain in SKIP_DOMAINS:
        return True
    for prefix in SKIP_PREFIXES:
        if local.startswith(prefix) or local == prefix.rstrip("@"):
            return True
    return False


def extract_name_parts(display_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname)."""
    parts = display_name.strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from an email domain."""
    base = domain.split(".")[0]  # e.g. 'example' from 'example.com'
    return base.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def build_hubspot_client():
    from hubspot import HubSpot

    if not HUBSPOT_ACCESS_TOKEN:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN is not set")
    return HubSpot(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact(client, email: str) -> dict | None:
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status", "hs_tag_ids"],
        limit=1,
    )
    result = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    if result.total > 0:
        return result.results[0]
    return None


def create_contact(client, email: str, firstname: str, lastname: str, company: str, subject: str) -> dict:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    props = {
        "email": email,
        "firstname": firstname or email.split("@")[0].title(),
        "lastname": lastname,
        "company": company,
        "hs_lead_status": "NEW",
        "lead_source_detail": GMAIL_SOURCE_TAG,
    }
    obj = SimplePublicObjectInputForCreate(properties=props)
    contact = client.crm.contacts.basic_api.create(simple_public_object_input_for_create=obj)
    _add_note(client, contact.id, email, subject, action="created")
    return contact


def update_contact(client, contact_id: str, email: str, firstname: str, lastname: str, company: str, subject: str) -> dict:
    from hubspot.crm.contacts import SimplePublicObjectInput

    props: dict[str, str] = {}
    # Only fill in fields that are not already set
    existing = client.crm.contacts.basic_api.get_by_id(
        contact_id,
        properties=["firstname", "lastname", "company", "lead_source_detail"],
    )
    ep = existing.properties

    if not ep.get("firstname"):
        props["firstname"] = firstname or email.split("@")[0].title()
    if not ep.get("lastname") and lastname:
        props["lastname"] = lastname
    if not ep.get("company") and company:
        props["company"] = company
    if not ep.get("lead_source_detail"):
        props["lead_source_detail"] = GMAIL_SOURCE_TAG

    if props:
        obj = SimplePublicObjectInput(properties=props)
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=obj,
        )

    _add_note(client, contact_id, email, subject, action="updated")
    return existing


def _add_note(client, contact_id: str, email: str, subject: str, action: str) -> None:
    """Create a HubSpot Note associated with the contact, logging the email event."""
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate
    from hubspot.crm.associations import PublicAssociation, AssociationSpec

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"[{GMAIL_SOURCE_TAG}] Email ricevuta il {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Da: {email}\n"
        f"Oggetto: {subject}\n"
        f"Azione: contatto {action}"
    )
    note_props = {
        "hs_note_body": body,
        "hs_timestamp": str(now_ms),
    }
    try:
        note_obj = SimplePublicObjectInputForCreate(properties=note_props)
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note_obj
        )
        # Associate note → contact
        client.crm.associations.v4.basic_api.create(
            object_type="notes",
            object_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_spec=[
                AssociationSpec(association_category="HUBSPOT_DEFINED", association_type_id=202)
            ],
        )
    except Exception as exc:  # non-fatal
        log.warning("Could not create note for contact %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def sync() -> list[dict]:
    service = build_gmail_service()
    client = build_hubspot_client()
    processed = load_state()

    raw_messages = fetch_messages(service)
    log.info("Fetched %d messages from Gmail", len(raw_messages))

    results: list[dict] = []

    for msg_stub in raw_messages:
        msg_id = msg_stub["id"]
        if msg_id in processed:
            log.debug("Already processed message %s, skipping", msg_id)
            continue

        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata", metadataHeaders=["From", "Subject"])
                .execute()
            )
        except Exception as exc:
            log.warning("Could not fetch message %s: %s", msg_id, exc)
            continue

        headers = msg.get("payload", {}).get("headers", [])
        from_header = get_header(headers, "From")
        subject = get_header(headers, "Subject")

        if not from_header:
            log.debug("No From header in message %s, skipping", msg_id)
            processed.add(msg_id)
            continue

        display_name, email, domain = parse_sender(from_header)

        if is_automated(email, domain):
            log.info("SKIP (automated)  %s", email)
            results.append({"status": "Ignorato", "email": email, "hubspot_id": None, "reason": "automated sender"})
            processed.add(msg_id)
            continue

        firstname, lastname = extract_name_parts(display_name)
        company = company_from_domain(domain)

        existing = find_contact(client, email)

        if existing is None:
            contact = create_contact(client, email, firstname, lastname, company, subject)
            status = "Creato"
            contact_id = contact.id
            log.info("CREATED  %s  (id=%s)", email, contact_id)
        else:
            contact_id = existing.id
            update_contact(client, contact_id, email, firstname, lastname, company, subject)
            status = "Aggiornato"
            log.info("UPDATED  %s  (id=%s)", email, contact_id)

        results.append({"status": status, "email": email, "hubspot_id": contact_id})
        processed.add(msg_id)

    save_state(processed)
    return results


def print_report(results: list[dict]) -> None:
    print("\n" + "=" * 60)
    print(f"{'STATO':<12} {'EMAIL':<40} {'HUBSPOT ID'}")
    print("-" * 60)
    for r in results:
        if r.get("reason") == "automated sender":
            continue
        print(f"{r['status']:<12} {r['email']:<40} {r.get('hubspot_id', '-')}")
    print("=" * 60)
    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    print(f"Riepilogo: Creati={created}  Aggiornati={updated}  Ignorati={ignored}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    results = sync()
    print_report(results)
