#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed estrae/sincronizza i contatti in HubSpot.
Evita duplicati usando l'email come chiave unica.

Uso:
    python gmail_hubspot_sync.py              # polling ogni 5 minuti
    python gmail_hubspot_sync.py --interval 0 # one-shot, poi esce
    python gmail_hubspot_sync.py --interval 60 # polling ogni minuto

Variabili d'ambiente richieste:
    HUBSPOT_ACCESS_TOKEN  — token privato HubSpot (Settings → Integrations → Private Apps)

File generati:
    token.json             — credenziali OAuth Gmail (auto-generato al primo avvio)
    processed_messages.json — ID messaggi già processati
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.utils import parseaddr

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate

# ─── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = "token.json"
GMAIL_CREDENTIALS_FILE = "credentials.json"   # scarica da Google Cloud Console

HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]

STATE_FILE = "processed_messages.json"

# Domini e prefissi locali automatici da ignorare
SKIP_DOMAINS = frozenset({
    "googlemail.com", "google.com", "googlegroups.com",
    "facebookmail.com", "facebook.com", "linkedin.com",
    "bounce.com", "amazonses.com", "sendgrid.net",
    "mailchimp.com", "mandrillapp.com", "notifications.google.com",
})
SKIP_LOCAL_PARTS = frozenset({
    "mailer-daemon", "postmaster", "noreply", "no-reply",
    "donotreply", "do-not-reply", "bounce", "notifications",
    "notification", "analytics-noreply", "support", "info",
})

# Dominio dell'account stesso (evita di aggiungere se stessi)
OWN_EMAIL_DOMAINS = frozenset({
    "latestata.it",
})

# HubSpot: valore per hs_analytics_source (enum obbligatorio)
HS_SOURCE = "OTHER_CAMPAIGNS"
NOTE_BODY = (
    "Email ricevuta via Gmail — Inbound Gmail.\n"
    "Contatto estratto automaticamente dalla sincronizzazione Gmail → HubSpot."
)

# ─── Gmail Auth ────────────────────────────────────────────────────────────────


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
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ─── State ─────────────────────────────────────────────────────────────────────


def load_state() -> set:
    if Path(STATE_FILE).exists():
        return set(json.loads(Path(STATE_FILE).read_text()))
    return set()


def save_state(processed: set):
    Path(STATE_FILE).write_text(json.dumps(sorted(processed), indent=2))


# ─── Email Parsing ─────────────────────────────────────────────────────────────


def is_automated(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    if domain in SKIP_DOMAINS:
        return True
    if domain in OWN_EMAIL_DOMAINS:
        return True
    local_base = local.split("+")[0]
    if local_base in SKIP_LOCAL_PARTS:
        return True
    if re.search(r"noreply|no-reply|donotreply", local, re.I):
        return True
    return False


def company_from_domain(domain: str) -> str:
    """Ricava nome azienda dal dominio, ignorando provider email generici."""
    generic = {
        "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "outlook.com",
        "libero.it", "alice.it", "virgilio.it", "tiscali.it", "tin.it",
        "pec.it", "legalmail.it",
    }
    if domain in generic:
        return ""
    name = domain.split(".")[0]
    return name.replace("-", " ").title()


def parse_sender(from_header: str) -> dict | None:
    """
    Analizza header From e restituisce dict con:
    email, firstname, lastname, company, domain
    oppure None se il mittente è automatico/da ignorare.
    """
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()
    if not email or "@" not in email:
        return None
    if is_automated(email):
        return None

    _, _, domain = email.partition("@")
    company = company_from_domain(domain)

    firstname = lastname = ""
    name = display_name.strip().strip('"')
    if name:
        parts = name.split(maxsplit=1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ─── HubSpot Helpers ───────────────────────────────────────────────────────────


def hs_client():
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact(client, email: str):
    """Cerca contatto per email; restituisce il record o None."""
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[Filter(
            property_name="email", operator="EQ", value=email
        )])],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0] if resp.total > 0 else None


def create_timeline_note(client, contact_id: str, subject: str):
    """Aggiunge una nota 'Inbound Gmail' nella timeline del contatto."""
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    note = client.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=NoteCreate(
            properties={
                "hs_note_body": NOTE_BODY,
                "hs_timestamp": str(ts),
            }
        )
    )
    # Associa nota → contatto
    client.crm.associations.v4.basic_api.create(
        object_type="notes",
        object_id=note.id,
        to_object_type="contacts",
        to_object_id=contact_id,
        association_spec=[{
            "associationCategory": "HUBSPOT_DEFINED",
            "associationTypeId": 202,
        }],
    )


def upsert_contact(client, sender: dict) -> dict:
    """
    Crea o aggiorna un contatto in HubSpot.
    Restituisce: {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "hubspot_id": ...}
    """
    email = sender["email"]
    existing = find_contact(client, email)

    if existing:
        ex = existing.properties
        updates = {}
        if sender["firstname"] and not ex.get("firstname"):
            updates["firstname"] = sender["firstname"]
        if sender["lastname"] and not ex.get("lastname"):
            updates["lastname"] = sender["lastname"]
        if sender["company"] and not ex.get("company"):
            updates["company"] = sender["company"]
        if not ex.get("hs_analytics_source"):
            updates["hs_analytics_source"] = HS_SOURCE

        if updates:
            client.crm.contacts.basic_api.update(
                contact_id=existing.id,
                simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
                    properties=updates
                ),
            )
            return {"status": "Aggiornato", "email": email, "hubspot_id": existing.id}
        return {"status": "Ignorato", "email": email, "hubspot_id": existing.id}

    # Nuovo contatto
    props = {
        "email": email,
        "hs_analytics_source": HS_SOURCE,
    }
    if sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if sender["company"]:
        props["company"] = sender["company"]

    contact = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=props
        )
    )
    # Nota timeline
    try:
        create_timeline_note(client, contact.id, email)
    except Exception as e:
        log.warning("Nota non creata per %s: %s", email, e)

    return {"status": "Creato", "email": email, "hubspot_id": contact.id}


# ─── Core Sync ─────────────────────────────────────────────────────────────────


def fetch_unprocessed(gmail, processed: set, max_results: int = 100) -> list:
    """Recupera messaggi inbox non ancora processati (esclude inviati e bozze)."""
    resp = gmail.users().messages().list(
        userId="me",
        q="in:inbox -from:me -is:draft",
        maxResults=max_results,
    ).execute()
    return [m for m in resp.get("messages", []) if m["id"] not in processed]


def process_message(gmail, client, msg_id: str) -> dict | None:
    """Recupera un messaggio, estrae il mittente, sincronizza in HubSpot."""
    msg = gmail.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From"],
    ).execute()

    from_raw = next(
        (h["value"] for h in msg.get("payload", {}).get("headers", [])
         if h["name"].lower() == "from"),
        None,
    )
    if not from_raw:
        return None

    sender = parse_sender(from_raw)
    if not sender:
        return None

    try:
        result = upsert_contact(client, sender)
        log.info("[%s] %s — HubSpot ID: %s", result["status"], result["email"], result["hubspot_id"])
        return result
    except ApiException as e:
        log.error("Errore HubSpot per %s: %s", sender["email"], e)
        return None


def run_sync(poll_interval: int = 300) -> list:
    """
    Loop principale.
    poll_interval=0  → one-shot (elabora una volta e termina)
    poll_interval>0  → polling continuo ogni N secondi
    """
    log.info("━━━ Gmail → HubSpot Contact Sync avviato ━━━")
    gmail = get_gmail_service()
    client = hs_client()
    processed = load_state()
    all_results: list[dict] = []

    while True:
        log.info("Controllo nuove email in arrivo...")
        new_msgs = fetch_unprocessed(gmail, processed)
        log.info("%d messaggi nuovi da processare", len(new_msgs))

        for msg in new_msgs:
            result = process_message(gmail, client, msg["id"])
            if result:
                all_results.append(result)
            processed.add(msg["id"])

        save_state(processed)
        _print_summary(all_results)

        if poll_interval <= 0:
            break
        log.info("Prossimo controllo tra %ds...", poll_interval)
        time.sleep(poll_interval)

    return all_results


def _print_summary(results: list[dict]):
    if not results:
        return
    print("\n" + "─" * 60)
    print(f"{'Stato':<12} {'Email':<40} {'HubSpot ID'}")
    print("─" * 60)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<40} {r['hubspot_id']}")
    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    skipped = sum(1 for r in results if r["status"] == "Ignorato")
    print("─" * 60)
    print(f"Totale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {skipped}")
    print("─" * 60 + "\n")


# ─── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Sincronizza mittenti email Gmail come contatti HubSpot"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        metavar="SECONDI",
        help="Secondi tra un controllo e l'altro. 0 = one-shot. Default: 300",
    )
    args = parser.parse_args()
    run_sync(poll_interval=args.interval)
