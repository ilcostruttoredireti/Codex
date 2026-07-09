"""
Gmail → HubSpot Contact Sync
Monitors inbound Gmail messages and creates/updates HubSpot contacts from real senders.
Requires: pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client hubspot-api-client python-dotenv
"""

import os
import re
import json
import base64
import datetime
from email.utils import parseaddr, getaddresses
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts import Filter, FilterGroup, PublicObjectSearchRequest
from hubspot.crm.engagements.notes import SimplePublicObjectInput as NoteInput

load_dotenv()

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")

# Patterns that identify automated / transactional senders to skip
SKIP_LOCAL_PATTERNS = re.compile(
    r"^(no[-_]?reply|donotreply|bounce|mailer[-_]?daemon|postmaster|noreply|"
    r"notification|newsletter|news|digest|alerts|store[-_]?news|sellersupport|"
    r"close[-_]?friend|googlebase|updates|info-noreply|support-noreply|"
    r"marketing|promo|campaigns|autoresponder|system|admin)@",
    re.IGNORECASE,
)

SKIP_DOMAINS = {
    "amazon.it", "amazon.com", "facebookmail.com", "linkedin.com",
    "google.com", "googleapis.com", "shop.tiktok.com", "tiktok.com",
    "skool.com", "circle.so", "serpapi.com", "thomsonreuters.com",
    "notification.circle.so", "accounts.google.com",
}


def is_automated(email: str) -> bool:
    domain = email.split("@")[-1].lower() if "@" in email else ""
    return bool(SKIP_LOCAL_PATTERNS.match(email)) or domain in SKIP_DOMAINS


def parse_sender(raw_from: str) -> tuple[str, str, str, str]:
    """Return (name, firstname, lastname, email) from a raw From header."""
    name, addr = parseaddr(raw_from)
    addr = addr.lower().strip()
    name = name.strip()
    parts = name.split(None, 1)
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""
    return name, firstname, lastname, addr


def domain_to_company(email: str) -> str:
    """Derive a human-readable company name from the email domain."""
    domain = email.split("@")[-1] if "@" in email else ""
    # Strip TLD and common prefixes, then title-case
    base = re.sub(r"\.(com|it|eu|net|org|io|co\.uk)$", "", domain, flags=re.I)
    base = re.sub(r"^(www|mail|info)\.", "", base, flags=re.I)
    return base.replace("-", " ").replace(".", " ").title() if base else ""


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_thread_ids": [], "last_run": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_gmail_service():
    creds = None
    token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_recent_inbox_senders(service, since_hours: int = 24) -> list[dict]:
    """Fetch unique senders from inbox messages in the last `since_hours` hours."""
    after_ts = int((datetime.datetime.utcnow() - datetime.timedelta(hours=since_hours)).timestamp())
    query = f"in:inbox -from:me after:{after_ts}"
    results = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = results.get("messages", [])

    seen_emails: set[str] = set()
    senders: list[dict] = []

    for msg_stub in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_stub["id"], format="metadata",
            metadataHeaders=["From", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        date_str = headers.get("Date", "")
        name, firstname, lastname, addr = parse_sender(raw_from)

        if not addr or addr in seen_emails or is_automated(addr):
            continue
        seen_emails.add(addr)
        senders.append({
            "email": addr,
            "firstname": firstname,
            "lastname": lastname,
            "name": name,
            "company": domain_to_company(addr),
            "domain": addr.split("@")[-1] if "@" in addr else "",
            "thread_id": msg.get("threadId", ""),
            "date": date_str,
            "message_id": msg_stub["id"],
        })
    return senders


def find_hubspot_contact(hs_client, email: str) -> dict | None:
    search_req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[
            Filter(property_name="email", operator="EQ", value=email)
        ])],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    resp = hs_client.crm.contacts.search_api.do_search(public_object_search_request=search_req)
    return resp.results[0] if resp.results else None


def upsert_contact(hs_client, sender: dict) -> tuple[str, str]:
    """
    Returns (status, hubspot_contact_id).
    status is one of: "Creato", "Aggiornato", "Ignorato"
    """
    existing = find_hubspot_contact(hs_client, sender["email"])

    props: dict[str, str] = {
        "email": sender["email"],
    }
    if sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if sender["company"]:
        props["company"] = sender["company"]

    if existing:
        contact_id = existing.id
        existing_props = existing.properties or {}
        updates: dict[str, str] = {}

        for key, val in props.items():
            if key == "email":
                continue
            if not existing_props.get(key):
                updates[key] = val

        if not existing_props.get("hs_lead_status"):
            updates["hs_lead_status"] = "NEW"

        if updates:
            hs_client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input={"properties": updates},
            )
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        props["hs_lead_status"] = "NEW"
        new_contact = hs_client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        contact_id = new_contact.id
        status = "Creato"

    return status, contact_id


def add_note(hs_client, contact_id: str, sender: dict) -> None:
    """Log an 'email received' note on the HubSpot contact timeline."""
    note_body = (
        f"Email inbound ricevuta da {sender['name'] or sender['email']} "
        f"il {sender['date']}.\nOggetto: (inbound Gmail)\nTag: Inbound Gmail"
    )
    note_props = {
        "hs_note_body": note_body,
        "hs_timestamp": str(int(datetime.datetime.utcnow().timestamp() * 1000)),
    }
    note_input = NoteInput(properties=note_props)
    note = hs_client.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=note_input
    )
    # Associate note with the contact
    hs_client.crm.associations.v4.basic_api.create(
        object_type="notes",
        object_id=note.id,
        to_object_type="contacts",
        to_object_id=contact_id,
        association_spec=[{
            "associationCategory": "HUBSPOT_DEFINED",
            "associationTypeId": 202,  # note → contact
        }],
    )


def run_sync(since_hours: int = 24) -> None:
    state = load_state()
    processed_ids: set[str] = set(state.get("processed_thread_ids", []))

    gmail = get_gmail_service()
    hs_client = hubspot.Client.create(access_token=HUBSPOT_TOKEN)

    senders = get_recent_inbox_senders(gmail, since_hours=since_hours)
    print(f"Trovati {len(senders)} mittenti reali da processare\n")

    results = []
    for sender in senders:
        if sender["thread_id"] in processed_ids:
            results.append({
                "stato": "Ignorato (già processato)",
                "email": sender["email"],
                "hubspot_id": "-",
            })
            continue
        try:
            status, contact_id = upsert_contact(hs_client, sender)
            if status in ("Creato", "Aggiornato"):
                add_note(hs_client, contact_id, sender)
            processed_ids.add(sender["thread_id"])
            results.append({
                "stato": status,
                "email": sender["email"],
                "hubspot_id": contact_id,
            })
        except ApiException as e:
            results.append({
                "stato": f"Errore: {e.status}",
                "email": sender["email"],
                "hubspot_id": "-",
            })
        except Exception as e:
            results.append({
                "stato": f"Errore: {e}",
                "email": sender["email"],
                "hubspot_id": "-",
            })

    print(f"{'Stato':<30} {'Email':<40} {'HubSpot ID'}")
    print("-" * 90)
    for r in results:
        print(f"{r['stato']:<30} {r['email']:<40} {r['hubspot_id']}")

    state["processed_thread_ids"] = list(processed_ids)
    state["last_run"] = datetime.datetime.utcnow().isoformat()
    save_state(state)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window in hours (default: 24)")
    args = parser.parse_args()
    run_sync(since_hours=args.hours)
