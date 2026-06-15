"""
Gmail → HubSpot Contact Sync
Legge le email in arrivo su Gmail, estrae i mittenti reali e li sincronizza in HubSpot.
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Configurazione ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path(__file__).parent / "token.json"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
PROCESSED_FILE = Path(__file__).parent / "processed_ids.json"

HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")

# Domini/pattern automatici da escludere
AUTOMATED_PATTERNS = re.compile(
    r"(no.?reply|noreply|mailer.daemon|postmaster|notifications?-noreply|"
    r"do.not.reply|donotreply|auto.?reply|bounce|newsletter|digest|bulk)",
    re.IGNORECASE,
)

AUTOMATED_DOMAINS = {
    "google.com", "googlemail.com", "gmail.com",
    "linkedin.com", "facebook.com", "twitter.com",
    "instagram.com", "tiktok.com",
    "fiverr.com", "upwork.com",
    "treatwell.it",
    "mailchimp.com", "sendgrid.net", "mandrillapp.com",
    "amazonses.com", "sparkpostmail.com",
}

# ── Caricamento ID già processati ──────────────────────────────────────────────

def load_processed() -> set:
    if PROCESSED_FILE.exists():
        return set(json.loads(PROCESSED_FILE.read_text()))
    return set()


def save_processed(ids: set):
    PROCESSED_FILE.write_text(json.dumps(sorted(ids)))


# ── Gmail ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, max_results: int = 50) -> list[dict]:
    """Restituisce i messaggi in arrivo non ancora processati."""
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def get_message_sender(service, msg_id: str) -> dict | None:
    """Restituisce {email, name, subject} del mittente o None se automatico."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    raw_from = headers.get("From", "")
    subject = headers.get("Subject", "")
    date = headers.get("Date", "")

    # Estrai email e nome dal campo From: "Nome <email@dominio.com>"
    match = re.match(r'(?:"?([^"<]+)"?\s+)?<?([^\s<>@]+@[^\s<>@]+)>?', raw_from.strip())
    if not match:
        return None

    display_name = (match.group(1) or "").strip().strip('"')
    email = match.group(2).strip().lower()

    # Filtra indirizzi automatici
    if AUTOMATED_PATTERNS.search(email):
        return None

    domain = email.split("@")[-1].lower()
    if domain in AUTOMATED_DOMAINS:
        return None

    return {"email": email, "name": display_name, "domain": domain, "subject": subject, "date": date}


# ── Parsing nome ───────────────────────────────────────────────────────────────

def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def company_from_domain(domain: str) -> str:
    """Ricava un nome azienda leggibile dal dominio (rimuove TLD e www)."""
    parts = domain.replace("www.", "").split(".")
    return parts[0].replace("-", " ").replace("_", " ").title() if parts else ""


# ── HubSpot ────────────────────────────────────────────────────────────────────

def get_hubspot_client():
    config = hubspot.Configuration(access_token=HUBSPOT_API_KEY)
    return hubspot.Client.create_with_api_client(config).crm.contacts


def find_contact(client, email: str) -> dict | None:
    """Cerca un contatto per email. Restituisce {id, properties} o None."""
    flt = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[flt])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
    )
    result = client.search_api.do_search(public_object_search_request=req)
    if result.total > 0:
        r = result.results[0]
        return {"id": r.id, "properties": r.properties}
    return None


def create_contact(client, sender: dict) -> str:
    """Crea un nuovo contatto HubSpot. Restituisce l'ID."""
    firstname, lastname = split_name(sender["name"])
    company = company_from_domain(sender["domain"])
    props = {
        "email": sender["email"],
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    obj = SimplePublicObjectInputForCreate(properties=props, associations=[])
    result = client.basic_api.create(simple_public_object_input_for_create=obj)
    return result.id


def update_contact(client, contact_id: str, sender: dict, existing: dict) -> bool:
    """Aggiorna i campi mancanti. Restituisce True se ha modificato qualcosa."""
    props = existing.get("properties", {})
    updates = {}

    if not props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    if not props.get("firstname") and sender["name"]:
        firstname, _ = split_name(sender["name"])
        if firstname:
            updates["firstname"] = firstname

    if not props.get("lastname") and sender["name"]:
        _, lastname = split_name(sender["name"])
        if lastname:
            updates["lastname"] = lastname

    if not props.get("company"):
        company = company_from_domain(sender["domain"])
        if company:
            updates["company"] = company

    if not updates:
        return False

    from hubspot.crm.contacts import SimplePublicObjectInput
    client.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return True


# ── Log attività HubSpot (nota) ────────────────────────────────────────────────

def add_gmail_note(hs_client, contact_id: str, sender: dict):
    """Crea una nota HubSpot per tracciare la ricezione dell'email."""
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
    body = (
        f"Inbound Gmail — Email ricevuta da {sender['email']}\n"
        f"Oggetto: {sender.get('subject', '')}\n"
        f"Data: {sender.get('date', '')}\n"
        f"Tag: Inbound Gmail | Fonte: Gmail"
    )
    note_obj = NoteCreate(
        properties={
            "hs_note_body": body,
            "hs_timestamp": datetime.now(timezone.utc).isoformat(),
        },
        associations=[
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    )
    hs_full = hubspot.Client.create(access_token=HUBSPOT_API_KEY)
    try:
        hs_full.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note_obj
        )
    except Exception as e:
        log.warning("Impossibile creare nota HubSpot: %s", e)


# ── Main sync ──────────────────────────────────────────────────────────────────

def run_sync():
    processed = load_processed()
    gmail = get_gmail_service()
    hs_contacts = get_hubspot_client()

    messages = fetch_inbox_messages(gmail)
    results = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed:
            continue

        sender = get_message_sender(gmail, msg_id)
        if sender is None:
            processed.add(msg_id)
            continue

        email = sender["email"]
        log.info("Elaborazione: %s", email)

        try:
            existing = find_contact(hs_contacts, email)

            if existing is None:
                contact_id = create_contact(hs_contacts, sender)
                add_gmail_note(hs_contacts, contact_id, sender)
                status = "Creato"
            else:
                contact_id = existing["id"]
                changed = update_contact(hs_contacts, contact_id, sender, existing)
                if changed:
                    add_gmail_note(hs_contacts, contact_id, sender)
                    status = "Aggiornato"
                else:
                    status = "Ignorato (dati già completi)"

        except ApiException as e:
            log.error("Errore HubSpot per %s: %s", email, e)
            status = f"Errore: {e}"
            contact_id = "N/A"

        results.append({"stato": status, "email": email, "hubspot_id": contact_id})
        processed.add(msg_id)

    save_processed(processed)

    print("\n── Risultati sincronizzazione Gmail → HubSpot ─────────────────────")
    print(f"{'Stato':<35} {'Email':<40} {'ID HubSpot'}")
    print("─" * 90)
    for r in results:
        print(f"{r['stato']:<35} {r['email']:<40} {r['hubspot_id']}")
    print(f"\nTotale elaborati: {len(results)}")
    return results


if __name__ == "__main__":
    run_sync()
