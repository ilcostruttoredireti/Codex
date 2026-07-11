"""
Gmail → HubSpot Contact Sync
=============================
Scans Gmail inbox for incoming emails, extracts sender info,
and creates or updates HubSpot contacts — deduped by email address.

Run:
    python gmail_hubspot_sync.py

Environment variables required:
    GMAIL_CREDENTIALS_JSON  – path to Gmail OAuth2 credentials file
    HUBSPOT_ACCESS_TOKEN    – HubSpot private-app access token

Optional:
    SYNC_LOOKBACK_DAYS      – how many days back to scan (default: 1)
    STATE_FILE              – path to JSON state file (default: .sync_state.json)
"""

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependencies: pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client
# ---------------------------------------------------------------------------

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    import hubspot
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
    from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
except ImportError as e:
    sys.exit(
        f"Missing dependency: {e}\n"
        "Install with: pip install google-auth google-auth-oauthlib "
        "google-api-python-client hubspot-api-client"
    )

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS = os.getenv("GMAIL_CREDENTIALS_JSON", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
LOOKBACK_DAYS = int(os.getenv("SYNC_LOOKBACK_DAYS", "1"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".sync_state.json"))

# Sender prefixes/domains that are automated/transactional — skip them.
SKIP_LOCAL_PARTS = {
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer", "notifications", "notify", "confirm", "alerts",
    "ads-noreply", "news", "newsletter", "updates", "info-noreply",
    "pinbot", "messaging-digest-noreply", "nobody", "robot",
}
SKIP_DOMAINS = {
    "google.com", "googlemail.com", "youtube.com", "gmail.com",
    "discord.com", "linkedin.com", "facebook.com", "instagram.com",
    "twitter.com", "x.com", "mailchimp.com", "sendgrid.net",
    "amazonses.com", "sparkpostmail.com", "em.salesforce.com",
    "vercel.com", "github.com", "slack.com",
}

# ---------------------------------------------------------------------------
# State: track already-processed thread IDs across runs
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_threads": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_recent_threads(service, lookback_days: int) -> list[dict]:
    """Return inbox threads received in the last `lookback_days` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
    query = f"in:inbox -from:me after:{since}"
    threads = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().threads().list(**kwargs).execute()
        threads.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return threads


def get_sender_from_thread(service, thread_id: str) -> tuple[str, str] | None:
    """Return (raw_name, email) from the first message in the thread, or None."""
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From"]
    ).execute()
    messages = thread.get("messages", [])
    if not messages:
        return None
    headers = {h["name"].lower(): h["value"] for h in messages[0].get("payload", {}).get("headers", [])}
    from_header = headers.get("from", "")
    raw_name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None
    return raw_name.strip(), email_addr.strip().lower()


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def should_skip(email: str) -> bool:
    local, _, domain = email.partition("@")
    if domain.lower() in SKIP_DOMAINS:
        return True
    if local.lower() in SKIP_LOCAL_PARTS:
        return True
    # Skip anything that looks like a noreply pattern
    if re.search(r"(no.?reply|norepl|donotreply|unsubscribe|bounce|daemon)", local, re.I):
        return True
    return False


# ---------------------------------------------------------------------------
# Name parsing helpers
# ---------------------------------------------------------------------------

def extract_name_parts(raw_name: str, email: str) -> tuple[str, str]:
    """Return (firstname, lastname) from the display name or email local part."""
    if raw_name:
        parts = raw_name.strip().split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        if len(parts) == 1:
            return parts[0], ""
    # Fall back to email local part
    local = email.split("@")[0]
    # Convert common separators to spaces
    cleaned = re.sub(r"[._\-+]", " ", local).strip().title()
    parts = cleaned.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return cleaned, ""


def extract_company(domain: str) -> str:
    """Best-effort company name from domain (strip TLD and common prefixes)."""
    parts = domain.split(".")
    # Remove www
    if parts[0].lower() == "www":
        parts = parts[1:]
    # Take the second-level domain as company
    company = parts[0] if parts else domain
    return company.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def get_hubspot_client():
    if not HUBSPOT_TOKEN:
        sys.exit("HUBSPOT_ACCESS_TOKEN environment variable is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact_by_email(client, email: str) -> dict | None:
    """Return existing HubSpot contact or None."""
    search_request = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[
                Filter(property_name="email", operator="EQ", value=email)
            ])
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=search_request)
    if resp.results:
        return resp.results[0]
    return None


def create_contact(client, email: str, firstname: str, lastname: str,
                   company: str) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (status, contact_id)."""
    props = {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "hs_lead_source": "OFFLINE",   # closest standard value; tag below
    }
    obj = SimplePublicObjectInputForCreate(properties=props)
    result = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=obj
    )
    return "Creato", result.id


def update_contact(client, contact_id: str, existing_props: dict,
                   firstname: str, lastname: str, company: str) -> tuple[str, str]:
    """Fill in any blank fields. Returns (status, contact_id)."""
    updates = {}
    if not existing_props.get("firstname") and firstname:
        updates["firstname"] = firstname
    if not existing_props.get("lastname") and lastname:
        updates["lastname"] = lastname
    if not existing_props.get("company") and company:
        updates["company"] = company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "OFFLINE"

    if not updates:
        return "Ignorato", contact_id

    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
            properties=updates
        ),
    )
    return "Aggiornato", contact_id


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def sync() -> list[dict]:
    state = load_state()
    processed_ids: set[str] = set(state.get("processed_threads", []))

    gmail = get_gmail_service()
    hs = get_hubspot_client()

    threads = fetch_recent_threads(gmail, LOOKBACK_DAYS)
    results = []

    for thread in threads:
        tid = thread["id"]
        if tid in processed_ids:
            continue

        sender = get_sender_from_thread(gmail, tid)
        if sender is None:
            processed_ids.add(tid)
            continue

        raw_name, email = sender
        if should_skip(email):
            processed_ids.add(tid)
            continue

        domain = email.split("@")[1]
        firstname, lastname = extract_name_parts(raw_name, email)
        company = extract_company(domain)

        try:
            existing = find_contact_by_email(hs, email)
            if existing:
                status, cid = update_contact(
                    hs, existing.id, existing.properties,
                    firstname, lastname, company
                )
            else:
                status, cid = create_contact(hs, email, firstname, lastname, company)
        except ApiException as exc:
            status, cid = f"Errore: {exc.status}", "—"

        results.append({"stato": status, "email": email, "hubspot_id": cid})
        processed_ids.add(tid)

    state["processed_threads"] = list(processed_ids)
    save_state(state)

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Avvio sync Gmail → HubSpot\n")
    results = sync()
    if not results:
        print("Nessuna nuova email da processare.")
    else:
        header = f"{'Stato':<12} {'Email':<45} {'HubSpot ID'}"
        print(header)
        print("-" * len(header))
        for r in results:
            print(f"{r['stato']:<12} {r['email']:<45} {r['hubspot_id']}")
    print(f"\nTotale processati: {len(results)}")
