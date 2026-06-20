"""
Gmail → HubSpot Contact Sync
=============================
Monitors Gmail inbox, extracts unique sender contacts and syncs them to HubSpot.

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2
                google-api-python-client hubspot-api-client python-dotenv

Environment variables (.env):
    HUBSPOT_ACCESS_TOKEN   - HubSpot private app token
    GMAIL_CREDENTIALS_JSON - Path to OAuth2 credentials.json downloaded from GCP
    GMAIL_TOKEN_JSON       - Path where the Gmail token will be stored (default: token.json)
    SYNC_LOOKBACK_HOURS    - How many hours back to scan (default: 24)
"""

import json
import os
import re
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_JSON", "credentials.json")
TOKEN_PATH = os.getenv("GMAIL_TOKEN_JSON", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
LOOKBACK_HOURS = int(os.getenv("SYNC_LOOKBACK_HOURS", "24"))

# Domains that belong to generic email providers — don't use as company name
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "icloud.com", "libero.it", "virgilio.it", "tin.it",
    "live.com", "me.com", "aol.com", "tiscali.it", "alice.it",
}

# Senders to skip entirely
SKIP_SENDERS = {
    "noreply", "no-reply", "mailer-daemon", "postmaster",
    "notification", "notifications", "facebookmail.com",
    "pageupdates@facebookmail.com",
}


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if Path(TOKEN_PATH).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, hours: int = 24) -> list[dict]:
    """Return a flat list of message metadata dicts from the inbox."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    # Gmail 'after' uses Unix timestamp
    after_ts = int(cutoff.timestamp())
    query = f"in:inbox -from:me after:{after_ts}"

    messages = []
    page_token = None
    while True:
        params = dict(userId="me", q=query, maxResults=500)
        if page_token:
            params["pageToken"] = page_token
        result = service.users().messages().list(**params).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message_headers(service, msg_id: str) -> dict:
    """Fetch only headers for a single message (minimal bandwidth)."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Reply-To"],
    ).execute()
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers


# ---------------------------------------------------------------------------
# Contact extraction helpers
# ---------------------------------------------------------------------------

def parse_sender(raw_from: str) -> tuple[str, str, str]:
    """
    Parse a RFC-2822 From header into (name, email, domain).
    Returns empty strings on failure.
    """
    name, address = parseaddr(raw_from)
    address = address.lower().strip()
    if not address or "@" not in address:
        return "", "", ""
    domain = address.split("@")[1]
    return name.strip(), address, domain


def should_skip(address: str, domain: str) -> bool:
    if not address:
        return True
    local = address.split("@")[0]
    if any(s in address for s in SKIP_SENDERS):
        return True
    if any(s in local for s in ("noreply", "no-reply", "mailer", "postmaster")):
        return True
    return False


def split_name(full_name: str) -> tuple[str, str]:
    """
    Best-effort split of a display name into (firstname, lastname).
    For organisation names (e.g. 'Ufficio Stampa WMF') returns (full_name, '').
    """
    parts = full_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    # Heuristic: if all parts are capitalised it looks like a person name
    if all(p[0].isupper() for p in parts if p):
        return parts[0], " ".join(parts[1:])
    return full_name, ""


def domain_to_company(domain: str) -> str:
    """Convert email domain to a human-readable company name guess."""
    if domain in GENERIC_DOMAINS:
        return ""
    # Strip TLD: foo.comune.sanseverinomarche.mc.it → "Comune Sanseverinomarche"
    parts = domain.split(".")
    # Remove known suffixes
    meaningful = [p for p in parts if p not in ("www", "mail", "smtp")]
    # Take up to first 3 parts, capitalise
    return " ".join(p.capitalize() for p in meaningful[:3])


def extract_contacts_from_messages(service, message_ids: list[str]) -> list[dict]:
    """Return a deduplicated list of contact dicts extracted from inbox messages."""
    seen: dict[str, dict] = {}  # email → contact dict

    for msg in message_ids:
        msg_id = msg["id"]
        try:
            headers = get_message_headers(service, msg_id)
        except Exception as exc:
            log.warning("Could not fetch message %s: %s", msg_id, exc)
            continue

        raw_from = headers.get("from", "") or headers.get("reply-to", "")
        name, email, domain = parse_sender(raw_from)

        if should_skip(email, domain):
            continue
        if email in seen:
            continue

        firstname, lastname = split_name(name) if name else ("", "")
        company = domain_to_company(domain)

        seen[email] = {
            "email": email,
            "firstname": firstname,
            "lastname": lastname,
            "company": company,
            "domain": domain,
        }

    return list(seen.values())


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def batch_lookup_contacts(client, emails: list[str]) -> dict[str, dict]:
    """Return {email: contact_record} for all emails that already exist."""
    if not emails:
        return {}

    existing: dict[str, dict] = {}
    chunk_size = 100  # HubSpot search max

    for i in range(0, len(emails), chunk_size):
        chunk = emails[i : i + chunk_size]
        f = Filter(property_name="email", operator="IN", values=chunk)
        fg = FilterGroup(filters=[f])
        req = PublicObjectSearchRequest(
            filter_groups=[fg],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=chunk_size,
        )
        try:
            resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
            for result in resp.results:
                em = (result.properties.get("email") or "").lower()
                if em:
                    existing[em] = result
        except ApiException as exc:
            log.error("HubSpot search error: %s", exc)

    return existing


def create_contact(client, contact: dict) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (status, hs_id)."""
    props = {
        "email": contact["email"],
        "hs_lead_status": "NEW",
        "leadsource": "Gmail",
    }
    if contact.get("firstname"):
        props["firstname"] = contact["firstname"]
    if contact.get("lastname"):
        props["lastname"] = contact["lastname"]
    if contact.get("company"):
        props["company"] = contact["company"]

    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return "Creato", result.id
    except ApiException as exc:
        body = json.loads(exc.body) if exc.body else {}
        if body.get("category") == "CONFLICT":
            # Duplicate detected by HubSpot itself
            return "Ignorato (duplicato)", ""
        log.error("Error creating %s: %s", contact["email"], exc)
        return "Errore", ""


def update_contact(client, hs_id: str, contact: dict, existing: dict) -> tuple[str, str]:
    """Fill in missing fields on an existing contact. Returns (status, hs_id)."""
    existing_props = existing.properties
    updates: dict[str, str] = {}

    # Only fill empty fields; never overwrite existing data
    if not existing_props.get("firstname") and contact.get("firstname"):
        updates["firstname"] = contact["firstname"]
    if not existing_props.get("lastname") and contact.get("lastname"):
        updates["lastname"] = contact["lastname"]
    if not existing_props.get("company") and contact.get("company"):
        updates["company"] = contact["company"]

    # Always ensure leadsource tag exists (doesn't overwrite non-Gmail sources)
    if not existing_props.get("leadsource"):
        updates["leadsource"] = "Gmail"

    if not updates:
        return "Ignorato (nessun aggiornamento)", hs_id

    try:
        from hubspot.crm.contacts import SimplePublicObjectInput
        client.crm.contacts.basic_api.update(
            contact_id=hs_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return "Aggiornato", hs_id
    except ApiException as exc:
        log.error("Error updating %s: %s", hs_id, exc)
        return "Errore", hs_id


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def run_sync(lookback_hours: int = LOOKBACK_HOURS) -> list[dict]:
    """
    Full sync pass. Returns a list of result records:
        [{"stato": ..., "email": ..., "hs_id": ...}, ...]
    """
    log.info("=== Gmail → HubSpot sync avviato (ultimi %d h) ===", lookback_hours)

    gmail = get_gmail_service()
    hs = get_hubspot_client()

    # 1. Fetch inbox messages
    log.info("Recupero messaggi Gmail...")
    messages = fetch_inbox_messages(gmail, hours=lookback_hours)
    log.info("Trovati %d messaggi nella inbox", len(messages))

    # 2. Extract unique contacts
    contacts = extract_contacts_from_messages(gmail, messages)
    log.info("Contatti unici estratti: %d", len(contacts))

    if not contacts:
        log.info("Nessun contatto da processare.")
        return []

    # 3. Batch check HubSpot
    emails = [c["email"] for c in contacts]
    existing_map = batch_lookup_contacts(hs, emails)
    log.info("Contatti già presenti in HubSpot: %d", len(existing_map))

    # 4. Create or update
    results = []
    for contact in contacts:
        email = contact["email"]
        if email in existing_map:
            stato, hs_id = update_contact(hs, existing_map[email].id, contact, existing_map[email])
        else:
            stato, hs_id = create_contact(hs, contact)

        results.append({"stato": stato, "email": email, "hs_id": hs_id})
        log.info("[%s] %s  (ID: %s)", stato, email, hs_id or "—")

    # 5. Summary
    created = sum(1 for r in results if r["stato"] == "Creato")
    updated = sum(1 for r in results if r["stato"] == "Aggiornato")
    ignored = sum(1 for r in results if r["stato"].startswith("Ignorato"))
    errors  = sum(1 for r in results if r["stato"] == "Errore")

    log.info(
        "=== Sync completato | Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d ===",
        created, updated, ignored, errors,
    )
    return results


if __name__ == "__main__":
    run_sync()
