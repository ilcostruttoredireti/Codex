"""
Gmail → HubSpot Contact Sync
Monitors inbox for new emails and syncs senders as HubSpot contacts.
"""

import os
import re
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "gmail_token.json"
CREDENTIALS_FILE = "gmail_credentials.json"

HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
HUBSPOT_BASE = "https://api.hubapi.com"

LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))

# Sender addresses to always skip (automated systems, noreply, etc.)
SKIP_PATTERNS = [
    r"noreply@",
    r"no-reply@",
    r"notify-noreply@",
    r"dont-reply@",
    r"donotreply@",
    r"mailer@",
    r"bounce@",
    r"nobody@",
    r"system@newsletter",
    r"conferma-spedizione@",
    r"premium@academia-mail",
    r"@amazon\.",
    r"@linkedin\.com",
    r"@skool\.com",
    r"@mailchimp\.com",
    r"adsense-noreply@",
    r"ads-noreply@",
    r"updates-noreply@",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_recent_senders(service, hours: int) -> list[dict]:
    """Return unique senders from the inbox in the last N hours."""
    after_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"in:inbox after:{after_ts} -from:me -in:sent"

    results = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=200)
        .execute()
    )
    messages = results.get("messages", [])

    seen_emails: set[str] = set()
    senders: list[dict] = []

    for msg_stub in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_stub["id"], format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        subject = headers.get("Subject", "")
        date_str = headers.get("Date", "")

        email, name = parse_from_header(raw_from)
        if not email or email in seen_emails:
            continue
        if should_skip(email):
            log.debug("Skip (automated): %s", email)
            continue

        seen_emails.add(email)
        senders.append({"email": email, "name": name, "subject": subject,
                        "date": date_str, "thread_id": msg.get("threadId", "")})

    return senders


def parse_from_header(raw: str) -> tuple[str, str]:
    """Extract (email, display_name) from a From header."""
    m = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', raw.strip())
    if m:
        return m.group(2).strip().lower(), m.group(1).strip().strip('"')
    m = re.match(r'<([^>]+)>', raw.strip())
    if m:
        return m.group(1).strip().lower(), ""
    if "@" in raw:
        return raw.strip().lower(), ""
    return "", ""


def should_skip(email: str) -> bool:
    return any(re.search(p, email, re.IGNORECASE) for p in SKIP_PATTERNS)


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def hs_search_contact(email: str) -> Optional[dict]:
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": email}
        ]}],
        "properties": ["email", "firstname", "lastname", "company",
                        "hs_analytics_source"],
        "limit": 1,
    }
    r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
                      headers=hs_headers(), json=body)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def derive_contact_fields(sender: dict) -> dict:
    """Best-effort extraction of name / company from sender data."""
    email = sender["email"]
    display_name = sender.get("name", "")
    domain = email.split("@")[-1] if "@" in email else ""
    company = domain_to_company(domain)

    firstname, lastname = "", ""
    if display_name:
        parts = display_name.split(maxsplit=1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""

    if not firstname:
        local = email.split("@")[0]
        if local not in ("info", "support", "hello", "general", "news",
                         "commerciale", "nobody", "noreply", "marketing",
                         "mondostudi", "premium", "confirm"):
            firstname = local.capitalize()

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "hs_analytics_source": "EMAIL_MARKETING",
    }


def domain_to_company(domain: str) -> str:
    """Best-effort company name from email domain."""
    known = {
        "miraconsulting.it": "MIRA Consulting",
        "martes-ai.com": "Martes AI",
        "mediacloud.press": "Media Cloud",
        "apollo.io": "Apollo.io",
        "selenebarletta.com": "Selene Barletta",
        "mircogasparotto.com": "Mirco Gasparotto",
        "rizzoaiacademy.com": "Rizzo AI Academy",
        "fatjoe.com": "FatJoe",
        "raffaprivatejet.com": "Raffa Private Jet",
        "rankpill.com": "RankPill",
        "diib.com": "Diib",
        "bdmassociati.it": "BDM Associati",
        "endercomunicazione.it": "Ender Comunicazione",
        "reconcc.it": "Cofidis / ReconCC",
        "ewww.io": "EWWW IO",
        "startupgeeks.it": "Startup Geeks",
        "thomsonreuters.com": "Thomson Reuters",
        "unsplash.com": "Unsplash",
        "semalt.org": "Semalt",
        "algolia.com": "Algolia",
    }
    # Try exact then subdomain strip
    if domain in known:
        return known[domain]
    parts = domain.split(".")
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in known:
            return known[candidate]
    # Fallback: prettify domain
    return domain.split(".")[0].replace("-", " ").replace("_", " ").title()


def hs_create_contact(fields: dict) -> dict:
    body = {"properties": {k: v for k, v in fields.items() if v}}
    r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
                      headers=hs_headers(), json=body)
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, fields: dict) -> dict:
    patch = {}
    for k, v in fields.items():
        if v and k != "email":
            patch[k] = v
    if not patch:
        return {}
    r = requests.patch(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
                       headers=hs_headers(), json={"properties": patch})
    r.raise_for_status()
    return r.json()


def hs_create_note(contact_id: str, body_text: str) -> dict:
    note = {
        "properties": {
            "hs_note_body": body_text,
            "hs_timestamp": str(int(time.time() * 1000)),
        }
    }
    r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/notes",
                      headers=hs_headers(), json=note)
    r.raise_for_status()
    note_id = r.json()["id"]

    # Associate note → contact
    assoc_url = (f"{HUBSPOT_BASE}/crm/v3/objects/notes/{note_id}"
                 f"/associations/contacts/{contact_id}/202")
    requests.put(assoc_url, headers=hs_headers())

    return r.json()


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def sync_once() -> list[dict]:
    log.info("=== Gmail → HubSpot sync started ===")
    service = get_gmail_service()
    senders = fetch_recent_senders(service, LOOKBACK_HOURS)
    log.info("Found %d unique senders to process", len(senders))

    results = []

    for sender in senders:
        email = sender["email"]
        try:
            existing = hs_search_contact(email)
            fields = derive_contact_fields(sender)

            if existing:
                contact_id = existing["id"]
                existing_props = existing.get("properties", {})

                # Only patch fields that are blank in HubSpot
                patch = {k: v for k, v in fields.items()
                         if v and not existing_props.get(k)}
                if patch:
                    hs_update_contact(contact_id, patch)

                status = "Aggiornato"
            else:
                created = hs_create_contact(fields)
                contact_id = created["id"]
                status = "Creato"

            # Always add a timeline note for this email
            note_body = (
                f"Inbound Gmail\n"
                f"Da: {email}\n"
                f"Oggetto: {sender.get('subject', '')}\n"
                f"Data: {sender.get('date', '')}\n"
                f"Tag: Inbound Gmail"
            )
            hs_create_note(contact_id, note_body)

            row = {
                "stato": status,
                "email_contatto": email,
                "id_hubspot": contact_id,
            }
            results.append(row)
            log.info("[%s]  %s  →  HubSpot ID %s", status, email, contact_id)

        except Exception as exc:
            log.error("Error processing %s: %s", email, exc)
            results.append({
                "stato": "Errore",
                "email_contatto": email,
                "id_hubspot": None,
                "errore": str(exc),
            })

    log.info("=== Sync complete: %d contacts processed ===", len(results))
    return results


if __name__ == "__main__":
    output = sync_once()
    print(json.dumps(output, indent=2, ensure_ascii=False))
