"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.
Uses sender email as the unique key to avoid duplicates.

Requirements:
  pip install google-auth google-auth-oauthlib google-auth-httplib2 \
              google-api-python-client hubspot-api-client python-dotenv

Environment variables (.env):
  HUBSPOT_API_KEY        — HubSpot private app token
  GMAIL_CREDENTIALS_FILE — path to OAuth2 credentials JSON
  GMAIL_TOKEN_FILE       — path to stored token JSON (created on first run)
  SYNC_INTERVAL_SECONDS  — polling interval (default: 300)
  LOOKBACK_HOURS         — how far back to look on first run (default: 24)
"""

import os
import re
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))

# Sender prefixes/patterns that are automated — skip them
SKIP_PREFIXES = (
    "no-reply", "noreply", "nobody", "mailer", "bounce",
    "donotreply", "do-not-reply", "notifications", "notify",
    "automailer", "auto-", "postmaster", "daemon",
)
SKIP_DOMAINS = {
    "facebookmail.com", "bounce.com", "amazonses.com",
}


def _is_automated(email: str) -> bool:
    local, domain = email.lower().split("@", 1)
    if domain in SKIP_DOMAINS:
        return True
    for prefix in SKIP_PREFIXES:
        if local.startswith(prefix):
            return True
    return False


def _parse_sender(raw_from: str) -> tuple[str, str, str]:
    """Return (display_name, email, domain)."""
    name, addr = parseaddr(raw_from)
    addr = addr.lower().strip()
    domain = addr.split("@")[-1] if "@" in addr else ""
    return name.strip(), addr, domain


def _company_from_domain(domain: str) -> str:
    """Guess company name from domain (e.g. moonshot.ai → Moonshot AI)."""
    root = domain.split(".")[0]
    return root.replace("-", " ").title()


def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _gmail_credentials() -> Credentials:
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return creds


def fetch_inbox_senders(since: datetime) -> list[dict]:
    """Return list of {name, email, domain} for inbox messages newer than since."""
    creds = _gmail_credentials()
    service = build("gmail", "v1", credentials=creds)
    since_epoch = int(since.timestamp())
    query = f"in:inbox after:{since_epoch} -in:draft -in:sent"

    seen: set[str] = set()
    results: list[dict] = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for msg_stub in resp.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=msg_stub["id"], format="metadata",
                metadataHeaders=["From"]
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            if not raw_from:
                continue
            name, email, domain = _parse_sender(raw_from)
            if not email or "@" not in email:
                continue
            if _is_automated(email):
                continue
            if email in seen:
                continue
            seen.add(email)
            results.append({"name": name, "email": email, "domain": domain})

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hubspot_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def _find_contact(client: hubspot.Client, email: str) -> dict | None:
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        return resp.results[0].to_dict() if resp.results else None
    except ApiException as exc:
        log.error("HubSpot search error for %s: %s", email, exc)
        return None


def _create_contact(client: hubspot.Client, email: str, name: str, domain: str) -> str | None:
    firstname, lastname = _split_name(name)
    company = _company_from_domain(domain)
    props = {
        "email": email,
        "firstname": firstname or email.split("@")[0].title(),
        "company": company,
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if lastname:
        props["lastname"] = lastname
    body = SimplePublicObjectInputForCreate(properties=props)
    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=body
        )
        return result.id
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", email, exc)
        return None


def _update_contact(client: hubspot.Client, contact_id: str, current: dict,
                    name: str, domain: str) -> bool:
    props = current.get("properties", {})
    updates: dict[str, str] = {}

    if not props.get("company"):
        updates["company"] = _company_from_domain(domain)
    if not props.get("firstname") and name:
        firstname, _ = _split_name(name)
        updates["firstname"] = firstname
    if not props.get("lastname") and name and " " in name:
        _, lastname = _split_name(name)
        if lastname:
            updates["lastname"] = lastname
    if not props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    if not updates:
        return False  # nothing to patch

    from hubspot.crm.contacts import SimplePublicObjectInput
    body = SimplePublicObjectInput(properties=updates)
    try:
        client.crm.contacts.basic_api.update(contact_id=contact_id,
                                              simple_public_object_input=body)
        return True
    except ApiException as exc:
        log.error("HubSpot update error for %s: %s", contact_id, exc)
        return False


# ── Main sync loop ────────────────────────────────────────────────────────────

def sync_once(since: datetime) -> list[dict]:
    """Process all new inbox senders since `since`. Returns processing log."""
    log.info("Fetching Gmail inbox senders since %s", since.isoformat())
    senders = fetch_inbox_senders(since)
    log.info("Found %d unique non-automated senders", len(senders))

    client = _hubspot_client()
    report: list[dict] = []

    for s in senders:
        email = s["email"]
        existing = _find_contact(client, email)

        if existing is None:
            contact_id = _create_contact(client, email, s["name"], s["domain"])
            status = "Creato" if contact_id else "Errore"
            log.info("[%s] %s → ID %s", status, email, contact_id)
            report.append({"stato": status, "email": email, "hubspot_id": contact_id})
        else:
            contact_id = existing["id"]
            updated = _update_contact(client, contact_id, existing, s["name"], s["domain"])
            status = "Aggiornato" if updated else "Ignorato"
            log.info("[%s] %s → ID %s", status, email, contact_id)
            report.append({"stato": status, "email": email, "hubspot_id": contact_id})

    return report


def main() -> None:
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    log.info("Gmail → HubSpot sync started (interval=%ds)", SYNC_INTERVAL)

    while True:
        now = datetime.now(timezone.utc)
        try:
            report = sync_once(since)
            print("\n--- Report sincronizzazione %s ---" % now.strftime("%Y-%m-%d %H:%M:%S UTC"))
            print(f"{'Stato':<12} {'Email':<40} {'HubSpot ID'}")
            print("-" * 70)
            for row in report:
                print(f"{row['stato']:<12} {row['email']:<40} {row['hubspot_id']}")
        except Exception as exc:
            log.exception("Sync iteration failed: %s", exc)

        since = now
        log.info("Next sync in %d seconds", SYNC_INTERVAL)
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
