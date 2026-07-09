"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot CRM.
Avoids duplicates using email as unique key; updates missing fields on existing contacts.
"""

import os
import re
import json
import base64
import logging
from dataclasses import dataclass, field
from typing import Optional
from email.utils import parseaddr

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Patterns that indicate automated/system senders to skip
SKIP_PATTERNS = re.compile(
    r"(noreply|no-reply|nobody@|notification|mailer@|donotreply|"
    r"bounce|autorespond|daemon@|postmaster@|admin@|"
    r"close_friend|sellersupport|googlebase|messaging-digest|"
    r"store_news@|cloudplatform|googlecloud@|facebookmail\.com|"
    r"shop\.tiktok|e\.feedspot|skool\.com|circle\.so|"
    r"email\.patreon|notification\.circle|moneya\.es|"
    r"account\.canva|serpapi\.com)",
    re.IGNORECASE,
)

LARGE_PLATFORM_DOMAINS = {
    "google.com", "gmail.com", "facebook.com", "amazon.com", "amazon.it",
    "tiktok.com", "linkedin.com", "twitter.com", "instagram.com",
    "youtube.com", "microsoft.com", "apple.com",
}


@dataclass
class ContactData:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""


def get_gmail_service(token_path="token.json", creds_path="credentials.json"):
    creds = None
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


def extract_domain(email: str) -> str:
    domain = email.split("@")[-1].lower()
    # Strip subdomains like "mailer." or "mail."
    parts = domain.split(".")
    if len(parts) > 2:
        domain = ".".join(parts[-2:])
    return domain


def company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (e.g. 'martes-ai.com' → 'Martes AI')."""
    name = domain.split(".")[0]
    name = name.replace("-", " ").replace("_", " ")
    return " ".join(w.capitalize() for w in name.split())


def parse_name_from_address(display_name: str, local_part: str) -> tuple[str, str]:
    """Return (first, last) from display name or local part of email."""
    if display_name:
        parts = display_name.strip().split(None, 1)
        return parts[0], parts[1] if len(parts) > 1 else ""
    # Guess from local part: "riccardo" → ("Riccardo", "")
    name = local_part.replace(".", " ").replace("_", " ").replace("-", " ")
    parts = name.split(None, 1)
    return parts[0].capitalize(), parts[1].capitalize() if len(parts) > 1 else ""


def should_skip(sender_email: str) -> bool:
    if SKIP_PATTERNS.search(sender_email):
        return True
    domain = extract_domain(sender_email)
    return domain in LARGE_PLATFORM_DOMAINS


def build_contact(raw_from: str) -> Optional[ContactData]:
    display_name, email = parseaddr(raw_from)
    if not email or "@" not in email:
        return None
    email = email.lower().strip()
    if should_skip(email):
        return None

    domain = extract_domain(email)
    local = email.split("@")[0]
    first, last = parse_name_from_address(display_name, local)
    company = company_from_domain(domain)

    return ContactData(
        email=email,
        first_name=first,
        last_name=last,
        company=company,
        domain=domain,
    )


def fetch_inbox_senders(service, max_results=50, query="in:inbox newer_than:1d -from:me") -> list[ContactData]:
    contacts: dict[str, ContactData] = {}
    response = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    messages = response.get("messages", [])

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        contact = build_contact(raw_from)
        if contact and contact.email not in contacts:
            contacts[contact.email] = contact

    return list(contacts.values())


def get_hubspot_contact(hs_client, email: str) -> Optional[dict]:
    try:
        results = hs_client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
                "properties": ["email", "firstname", "lastname", "company", "lead_source"],
                "limit": 1,
            }
        )
        if results.results:
            return results.results[0]
    except ApiException as e:
        log.error("HubSpot search error for %s: %s", email, e)
    return None


def sync_contact(hs_client, contact: ContactData) -> tuple[str, str, str]:
    """
    Returns (status, email, hubspot_id).
    status: 'Creato' | 'Aggiornato' | 'Ignorato'
    """
    existing = get_hubspot_contact(hs_client, contact.email)

    props = {
        "email": contact.email,
        "firstname": contact.first_name,
        "lastname": contact.last_name,
        "company": contact.company,
        "lead_source": "Gmail",
        "hs_lead_status": "NEW",
    }
    # Strip empty values
    props = {k: v for k, v in props.items() if v}

    if existing is None:
        # Create new contact
        try:
            result = hs_client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
            )
            log.info("CREATO  %s → ID %s", contact.email, result.id)
            return "Creato", contact.email, str(result.id)
        except ApiException as e:
            log.error("Create failed for %s: %s", contact.email, e)
            return "Errore", contact.email, ""

    # Contact exists — update only missing fields
    existing_props = existing.properties or {}
    updates = {
        k: v for k, v in props.items()
        if k != "email" and not existing_props.get(k)
    }
    if not updates:
        log.info("IGNORATO %s (ID %s, nessun campo mancante)", contact.email, existing.id)
        return "Ignorato", contact.email, str(existing.id)

    try:
        hs_client.crm.contacts.basic_api.update(
            contact_id=existing.id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        log.info("AGGIORNATO %s (ID %s) campi: %s", contact.email, existing.id, list(updates))
        return "Aggiornato", contact.email, str(existing.id)
    except ApiException as e:
        log.error("Update failed for %s: %s", contact.email, e)
        return "Errore", contact.email, str(existing.id)


def run_sync(
    gmail_token="token.json",
    gmail_creds="credentials.json",
    hubspot_token=None,
    query="in:inbox newer_than:1d -from:me",
):
    hubspot_token = hubspot_token or os.environ["HUBSPOT_ACCESS_TOKEN"]
    gmail_svc = get_gmail_service(token_path=gmail_token, creds_path=gmail_creds)
    hs_client = hubspot.Client.create(access_token=hubspot_token)

    contacts = fetch_inbox_senders(gmail_svc, query=query)
    log.info("Trovati %d mittenti unici da processare", len(contacts))

    results = []
    for c in contacts:
        status, email, hs_id = sync_contact(hs_client, c)
        results.append({"stato": status, "email": email, "hubspot_id": hs_id})

    # Summary
    from collections import Counter
    counts = Counter(r["stato"] for r in results)
    log.info(
        "Sync completata — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d",
        counts["Creato"], counts["Aggiornato"], counts["Ignorato"], counts["Errore"],
    )
    return results


if __name__ == "__main__":
    import sys
    results = run_sync()
    for r in results:
        print(f"{r['stato']:<12} {r['email']:<45} {r['hubspot_id']}")
    sys.exit(0)
