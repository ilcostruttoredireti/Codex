"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed estrae i mittenti come contatti HubSpot.
Evita duplicati usando l'email come chiave univoca. Aggiorna i campi mancanti.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

# Requires:
#   pip install google-auth-oauthlib google-api-python-client hubspot-api-client

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput

# ─── Config ─────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = "gmail_credentials.json"  # OAuth2 client secrets
GMAIL_TOKEN_FILE = "gmail_token.json"              # Cached user token

HUBSPOT_ACCESS_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL_SECONDS = 300   # Check every 5 minutes
STATE_FILE = "sync_state.json"  # Tracks processed Gmail thread IDs

# Domains/senders to skip (automated/system)
SKIP_SENDERS = {
    "no-reply@accounts.google.com",
    "mailer-daemon@googlemail.com",
    "mailer-daemon@googlemail.com",
    "noreply@google.com",
    "notifications@google.com",
    "notification@priority.facebookmail.com",
}

# Own account emails to skip
OWN_EMAILS: set[str] = set()  # Populated at runtime from authenticated Gmail user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sync.log"),
    ],
)
log = logging.getLogger(__name__)


# ─── Gmail helpers ───────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
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


def get_user_email(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"].lower()


def fetch_new_threads(service, processed_ids: set[str]) -> list[dict]:
    """Return inbox threads not yet processed."""
    result = service.users().threads().list(
        userId="me",
        q="in:inbox -from:me",
        maxResults=100,
    ).execute()

    threads = result.get("threads", [])
    new_threads = []
    for t in threads:
        if t["id"] not in processed_ids:
            full = service.users().threads().get(
                userId="me", id=t["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            new_threads.append(full)
    return new_threads


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Return (email, firstname, lastname) from a From: header."""
    email = ""
    display_name = ""

    # "Display Name <email@domain>"
    match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>', from_header.strip())
    if match:
        display_name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        # bare email
        email = from_header.strip().lower()

    parts = display_name.split() if display_name else []
    firstname = parts[0].title() if parts else ""
    lastname = " ".join(p.title() for p in parts[1:]) if len(parts) > 1 else ""
    return email, firstname, lastname


def extract_domain(email: str) -> str:
    return email.split("@")[-1] if "@" in email else ""


def domain_to_company(domain: str) -> str:
    """Best-effort company name from domain."""
    if not domain:
        return ""
    # Strip TLD(s) and capitalise
    name = domain.rsplit(".", 2)[0] if domain.count(".") >= 2 else domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def get_thread_sender(thread: dict) -> tuple[str, str, str]:
    """Return (email, firstname, lastname) for the first message in the thread."""
    messages = thread.get("messages", [])
    if not messages:
        return "", "", ""
    headers = {h["name"]: h["value"] for h in messages[0].get("payload", {}).get("headers", [])}
    return parse_sender(headers.get("From", ""))


# ─── HubSpot helpers ─────────────────────────────────────────────────────────

def get_hubspot_client() -> HubSpot:
    if not HUBSPOT_ACCESS_TOKEN:
        raise RuntimeError("Set HUBSPOT_ACCESS_TOKEN environment variable")
    return HubSpot(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(client: HubSpot, email: str) -> dict | None:
    """Return the HubSpot contact dict if found, else None."""
    from hubspot.crm.contacts import Filter, FilterGroup, PublicObjectSearchRequest

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.total > 0:
        return resp.results[0]
    return None


def create_contact(client: HubSpot, props: dict) -> str:
    """Create a new contact and return its HubSpot ID."""
    obj = SimplePublicObjectInputForCreate(properties=props)
    result = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=obj
    )
    return result.id


def update_contact(client: HubSpot, contact_id: str, props: dict) -> None:
    """Patch a contact with only the provided properties."""
    obj = SimplePublicObjectInput(properties=props)
    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=obj,
    )


# ─── State persistence ───────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_thread_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Core processing ─────────────────────────────────────────────────────────

def process_thread(
    thread: dict,
    hs_client: HubSpot,
    own_emails: set[str],
) -> dict:
    """
    Process one Gmail thread, sync sender to HubSpot.

    Returns:
        {"status": "Created"|"Updated"|"Ignored", "email": str, "hubspot_id": str}
    """
    email, firstname, lastname = get_thread_sender(thread)

    if not email:
        return {"status": "Ignored", "email": "", "hubspot_id": "", "reason": "no sender"}

    if email in SKIP_SENDERS or email in own_emails:
        return {"status": "Ignored", "email": email, "hubspot_id": "", "reason": "skip list"}

    # Automated senders: no-reply, noreply, mailer-daemon, notifications
    local = email.split("@")[0]
    if any(x in local for x in ("no-reply", "noreply", "mailer-daemon", "notification", "bounce", "do-not-reply")):
        return {"status": "Ignored", "email": email, "hubspot_id": "", "reason": "automated sender"}

    domain = extract_domain(email)
    company = domain_to_company(domain)

    existing = find_contact_by_email(hs_client, email)

    if existing:
        contact_id = existing.id
        props_to_update: dict[str, str] = {}
        ep = existing.properties

        if not ep.get("firstname") and firstname:
            props_to_update["firstname"] = firstname
        if not ep.get("lastname") and lastname:
            props_to_update["lastname"] = lastname
        if not ep.get("company") and company:
            props_to_update["company"] = company
        if not ep.get("hs_lead_source"):
            props_to_update["hs_lead_source"] = "OTHER_CAMPAIGNS"

        if props_to_update:
            update_contact(hs_client, contact_id, props_to_update)
            log.info("UPDATED  %s (id=%s) — %s", email, contact_id, list(props_to_update))
        else:
            log.info("IGNORED  %s (id=%s) — already complete", email, contact_id)

        return {"status": "Updated" if props_to_update else "Ignored",
                "email": email, "hubspot_id": contact_id}

    # Create new contact
    props: dict[str, str] = {
        "email": email,
        "hs_lead_source": "OTHER_CAMPAIGNS",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    try:
        contact_id = create_contact(hs_client, props)
        log.info("CREATED  %s (id=%s)", email, contact_id)
        return {"status": "Created", "email": email, "hubspot_id": contact_id}
    except ApiException as exc:
        if exc.status == 409:
            # Race-condition duplicate; fetch and update instead
            existing2 = find_contact_by_email(hs_client, email)
            if existing2:
                return {"status": "Ignored", "email": email, "hubspot_id": existing2.id,
                        "reason": "duplicate (conflict)"}
        log.error("HubSpot API error for %s: %s", email, exc)
        return {"status": "Ignored", "email": email, "hubspot_id": "", "reason": str(exc)}


# ─── Main loop ───────────────────────────────────────────────────────────────

def run():
    log.info("Starting Gmail → HubSpot sync (poll every %ds)", POLL_INTERVAL_SECONDS)

    gmail = get_gmail_service()
    own_email = get_user_email(gmail)
    OWN_EMAILS.add(own_email)
    log.info("Authenticated Gmail user: %s", own_email)

    hs_client = get_hubspot_client()

    state = load_state()
    processed = set(state["processed_thread_ids"])

    while True:
        log.info("Checking for new threads …")
        try:
            threads = fetch_new_threads(gmail, processed)
            log.info("Found %d new thread(s)", len(threads))

            for thread in threads:
                tid = thread["id"]
                result = process_thread(thread, hs_client, OWN_EMAILS)

                # Print output row
                print(
                    f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] "
                    f"{result['status']:8s} | {result.get('email',''):45s} | "
                    f"HubSpot ID: {result.get('hubspot_id','N/A')}"
                )

                processed.add(tid)

            state["processed_thread_ids"] = list(processed)
            state["last_run"] = datetime.now(timezone.utc).isoformat()
            save_state(state)

        except Exception as exc:
            log.error("Error during sync cycle: %s", exc, exc_info=True)

        log.info("Sleeping %ds until next check …", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
