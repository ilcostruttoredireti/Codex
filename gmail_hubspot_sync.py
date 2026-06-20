"""
Gmail → HubSpot Contact Sync
Monitora la inbox Gmail e sincronizza i mittenti come contatti HubSpot.
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

import httpx
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ── Costanti ───────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_API_BASE = "https://api.hubapi.com"

# Domini generici da cui non si ricava il nome azienda
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "libero.it", "virgilio.it", "tiscali.it", "live.com",
    "icloud.com", "me.com", "protonmail.com",
}

# Email di sistema da ignorare
IGNORED_SENDER_PATTERNS = [
    r"noreply", r"no-reply", r"donotreply", r"notification",
    r"mailer-daemon", r"postmaster",
]


# ── Helper ──────────────────────────────────────────────────────────────────────

def is_system_email(email: str) -> bool:
    email_lower = email.lower()
    return any(re.search(p, email_lower) for p in IGNORED_SENDER_PATTERNS)


def extract_name_parts(display_name: str) -> tuple[str, str]:
    """Divide un nome visualizzato in (firstname, lastname)."""
    parts = display_name.strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], ""


def company_from_domain(domain: str) -> Optional[str]:
    """Estrae nome azienda dal dominio se non è un provider generico."""
    if domain.lower() in GENERIC_DOMAINS:
        return None
    # "musicandmediapress.it" → "Musicandmediapress"
    name = domain.split(".")[0].replace("-", " ").title()
    return name


# ── Gmail ───────────────────────────────────────────────────────────────────────

def build_gmail_service():
    creds = None
    token_path = os.environ.get("GMAIL_TOKEN_PATH", "token.json")
    creds_path = os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Token Gmail non valido. Esegui l'autenticazione iniziale."
            )
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_recent_senders(service, hours: int = 24) -> list[dict]:
    """Restituisce lista di {email, name, domain} dei mittenti unici delle ultime N ore."""
    query = f"in:inbox newer_than:{hours}h -from:me"
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=100)
        .execute()
    )
    messages = result.get("messages", [])

    seen: dict[str, dict] = {}
    for msg_meta in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_meta["id"], format="metadata",
                 metadataHeaders=["From", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        from_header = headers.get("From", "")
        display_name, email_addr = parseaddr(from_header)
        email_addr = email_addr.lower().strip()

        if not email_addr or email_addr in seen or is_system_email(email_addr):
            continue

        domain = email_addr.split("@")[-1] if "@" in email_addr else ""
        seen[email_addr] = {
            "email": email_addr,
            "display_name": display_name,
            "domain": domain,
        }

    return list(seen.values())


# ── HubSpot ─────────────────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
        body = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
            "limit": 1,
        }
        r = httpx.post(url, headers=self._headers, json=body, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
        r = httpx.post(url, headers=self._headers, json={"properties": props}, timeout=15)
        r.raise_for_status()
        return r.json()

    def update_contact(self, contact_id: str, props: dict) -> dict:
        url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
        r = httpx.patch(url, headers=self._headers, json={"properties": props}, timeout=15)
        r.raise_for_status()
        return r.json()

    def add_note(self, contact_id: str, body: str) -> dict:
        url = f"{HUBSPOT_API_BASE}/crm/v3/objects/notes"
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        note_data = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(ts),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                }
            ],
        }
        r = httpx.post(url, headers=self._headers, json=note_data, timeout=15)
        r.raise_for_status()
        return r.json()


# ── Logica di sync ───────────────────────────────────────────────────────────────

def build_contact_props(sender: dict) -> dict:
    firstname, lastname = extract_name_parts(sender["display_name"]) if sender["display_name"] else ("", "")
    company = company_from_domain(sender["domain"])

    props: dict = {"email": sender["email"]}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    return props


def sync_sender(hs: HubSpotClient, sender: dict) -> dict:
    email = sender["email"]
    existing = hs.find_contact_by_email(email)
    desired = build_contact_props(sender)
    note_body = (
        f"Email inbound ricevuta via Gmail il {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}. "
        f"Tag: Inbound Gmail"
    )

    if existing:
        contact_id = existing["id"]
        current = existing.get("properties", {})
        updates = {
            k: v for k, v in desired.items()
            if v and not current.get(k)
        }
        if updates:
            hs.update_contact(contact_id, updates)
            status = "AGGIORNATO"
        else:
            status = "IGNORATO"
        hs.add_note(contact_id, note_body)
        return {"status": status, "email": email, "hubspot_id": contact_id}
    else:
        created = hs.create_contact(desired)
        contact_id = created["id"]
        hs.add_note(contact_id, note_body)
        return {"status": "CREATO", "email": email, "hubspot_id": contact_id}


# ── Entry point ──────────────────────────────────────────────────────────────────

def main(hours: int = 24):
    hubspot_token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    hs = HubSpotClient(hubspot_token)
    gmail = build_gmail_service()

    log.info("Recupero email dalle ultime %dh…", hours)
    senders = fetch_recent_senders(gmail, hours=hours)
    log.info("Trovati %d mittenti unici da processare.", len(senders))

    results = []
    for sender in senders:
        try:
            result = sync_sender(hs, sender)
            results.append(result)
            log.info("%-10s | %-45s | ID: %s", result["status"], result["email"], result["hubspot_id"])
        except Exception as exc:
            log.error("Errore per %s: %s", sender["email"], exc)
            results.append({"status": "ERRORE", "email": sender["email"], "hubspot_id": None})

    created = sum(1 for r in results if r["status"] == "CREATO")
    updated = sum(1 for r in results if r["status"] == "AGGIORNATO")
    ignored = sum(1 for r in results if r["status"] == "IGNORATO")
    errors  = sum(1 for r in results if r["status"] == "ERRORE")

    log.info("── Riepilogo ──────────────────────────────")
    log.info("Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d", created, updated, ignored, errors)
    return results


if __name__ == "__main__":
    import sys
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    main(hours)
