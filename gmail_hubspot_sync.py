"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot CRM.
Avoids duplicates using email as unique key.
"""

import os
import re
import json
import time
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --- Configuration ---
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDENTIALS_FILE = "gmail_credentials.json"
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"
STATE_FILE = "sync_state.json"

# Patterns to skip (automated / no-reply senders)
SKIP_PATTERNS = re.compile(
    r"^(no-?reply|noreply|mailer-daemon|notifications?|alerts?|bounce|"
    r"security|system|admin|support@(shop\.|sc\.)|donotreply|"
    r"facebookmail|googleplay|accounts\.google)",
    re.IGNORECASE,
)
SKIP_DOMAINS = {
    "facebookmail.com", "googlemail.com", "accounts.google.com",
    "notify.cloudflare.com", "moneya.es", "revolut.com",
    "fastweb.it", "serpapi.com", "algolia.com", "airtable.com",
    "fatturazioneelettronica.aruba.it", "sc.mail.deepseek.com",
    "marketing.base44.com", "notification.circle.so", "kaggle.com",
    "youtube.com", "blotato.com", "openzeppelin.com", "vercel.com",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------- Gmail helpers ----------

def get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_history_id": None, "processed_message_ids": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_new_messages(service, state):
    """Return list of (message_id, sender_raw, subject) for unprocessed inbox messages."""
    processed = set(state.get("processed_message_ids", []))
    messages = []
    page_token = None
    while True:
        params = {
            "userId": "me",
            "labelIds": ["INBOX"],
            "q": "-from:me",
            "maxResults": 100,
        }
        if page_token:
            params["pageToken"] = page_token
        result = service.users().messages().list(**params).execute()
        for msg in result.get("messages", []):
            if msg["id"] not in processed:
                messages.append(msg["id"])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message_sender(service, message_id):
    msg = service.users().messages().get(
        userId="me", id=message_id, format="metadata",
        metadataHeaders=["From", "Subject"]
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    return headers.get("From", ""), headers.get("Subject", "")


# ---------- Sender parsing ----------

def parse_sender(raw_from):
    name, email = parseaddr(raw_from)
    email = email.strip().lower()
    if not email or "@" not in email:
        return None, None, None
    domain = email.split("@")[1]
    return name.strip(), email, domain


def should_skip(email, domain):
    if SKIP_PATTERNS.match(email.split("@")[0]):
        return True
    if domain in SKIP_DOMAINS:
        return True
    return False


def infer_name_from_email(email):
    local = email.split("@")[0]
    local = re.sub(r"[._\-+]", " ", local)
    parts = [p.capitalize() for p in local.split() if len(p) > 1]
    return parts[0] if parts else "", " ".join(parts[1:]) if len(parts) > 1 else ""


def infer_company(domain):
    base = domain.split(".")[0]
    return base.replace("-", " ").title()


# ---------- HubSpot helpers ----------

def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def find_contact_by_email(client, email):
    flt = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[flt])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        res = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if res.total > 0:
            return res.results[0]
    except ApiException as e:
        log.error("HubSpot search error: %s", e)
    return None


def create_contact(client, firstname, lastname, email, company):
    props = {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "leadsource": CONTACT_SOURCE,
    }
    obj = SimplePublicObjectInputForCreate(properties=props)
    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        return result.id
    except ApiException as e:
        log.error("HubSpot create error for %s: %s", email, e)
        return None


def update_contact(client, contact_id, updates):
    from hubspot.crm.contacts import SimplePublicObjectInput
    obj = SimplePublicObjectInput(properties=updates)
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id, simple_public_object_input=obj
        )
        return True
    except ApiException as e:
        log.error("HubSpot update error for id %s: %s", contact_id, e)
        return False


# ---------- Main sync ----------

def process_email(service, hs_client, message_id, state):
    raw_from, subject = get_message_sender(service, message_id)
    name, email, domain = parse_sender(raw_from)

    if not email:
        return {"status": "Ignorato", "email": raw_from, "hubspot_id": None,
                "reason": "Indirizzo email non valido"}

    if should_skip(email, domain):
        return {"status": "Ignorato", "email": email, "hubspot_id": None,
                "reason": "Mittente automatico/no-reply"}

    existing = find_contact_by_email(hs_client, email)

    if existing:
        # Update missing fields
        updates = {}
        props = existing.properties
        if not props.get("leadsource"):
            updates["leadsource"] = CONTACT_SOURCE
        if not props.get("company"):
            updates["company"] = infer_company(domain)

        if updates:
            update_contact(hs_client, existing.id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"

        return {"status": status, "email": email, "hubspot_id": existing.id}

    # Create new contact
    if name:
        parts = name.split(None, 1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""
    else:
        firstname, lastname = infer_name_from_email(email)

    company = infer_company(domain)
    new_id = create_contact(hs_client, firstname, lastname, email, company)

    if new_id:
        return {"status": "Creato", "email": email, "hubspot_id": new_id}
    return {"status": "Errore", "email": email, "hubspot_id": None}


def run_sync():
    state = load_state()
    service = get_gmail_service()
    hs_client = get_hubspot_client()

    log.info("Fetching new inbox messages...")
    new_ids = fetch_new_messages(service, state)
    log.info("Found %d unprocessed messages", len(new_ids))

    results = []
    processed = set(state.get("processed_message_ids", []))

    for msg_id in new_ids:
        result = process_email(service, hs_client, msg_id, state)
        results.append(result)
        processed.add(msg_id)
        log.info("[%s] %s → HubSpot ID: %s", result["status"], result["email"],
                 result.get("hubspot_id", "-"))
        time.sleep(0.2)  # respect API rate limits

    # Persist processed IDs (keep last 5000 to bound file size)
    state["processed_message_ids"] = list(processed)[-5000:]
    save_state(state)

    # Summary
    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    log.info(
        "Sync completato: %d creati, %d aggiornati, %d ignorati, %d errori",
        counts["Creato"], counts["Aggiornato"], counts["Ignorato"], counts.get("Errore", 0),
    )
    return results


if __name__ == "__main__":
    run_sync()
