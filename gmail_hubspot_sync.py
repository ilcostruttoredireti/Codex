#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Monitora la casella Gmail, estrae i mittenti reali e li sincronizza
come contatti in HubSpot (crea nuovi o aggiorna campi mancanti).

Uso:
    python gmail_hubspot_sync.py [--hours 24]

Variabili d'ambiente richieste (vedi .env.example):
    HUBSPOT_API_KEY            Private App token HubSpot
    GMAIL_CREDENTIALS_PATH     Percorso credentials.json OAuth2 Google
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Prefissi locali delle email automatiche da ignorare
SKIP_LOCAL_PATTERNS = re.compile(
    r"^(no[-_]?reply|noreply|notify|notification|newsletter|mailer|nobody|"
    r"friends|invitations|dailybriefing|premium|headway|cloudplatform|"
    r"notify-noreply|do-not-reply|donotreply|bounce|postmaster|admin|"
    r"auto-?reply|support-noreply)$",
    re.IGNORECASE,
)

# Domini generici per cui non derivare il nome azienda
GENERIC_DOMAINS = frozenset(
    ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
     "icloud.com", "protonmail.com", "libero.it", "virgilio.it"]
)

# File locale per tracciare i message ID già processati (evita doppi)
PROCESSED_IDS_FILE = Path(".processed_ids.json")


# ---------------------------------------------------------------------------
# Funzioni di utilità
# ---------------------------------------------------------------------------

def load_processed_ids() -> set[str]:
    if PROCESSED_IDS_FILE.exists():
        return set(json.loads(PROCESSED_IDS_FILE.read_text()))
    return set()


def save_processed_ids(ids: set[str]) -> None:
    PROCESSED_IDS_FILE.write_text(json.dumps(sorted(ids)))


def is_automated(email: str) -> bool:
    local = email.split("@")[0]
    return bool(SKIP_LOCAL_PATTERNS.match(local))


def company_from_domain(domain: str) -> str:
    if not domain or domain.lower() in GENERIC_DOMAINS:
        return ""
    base = domain.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


def split_name(display_name: str, email: str) -> tuple[str, str]:
    if display_name and not display_name.strip().startswith(email.split("@")[0]):
        parts = display_name.strip().split(None, 1)
        return parts[0], parts[1] if len(parts) > 1 else ""
    local = email.split("@")[0]
    cleaned = re.sub(r"[\.\-_]", " ", local).title()
    return cleaned, ""


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------

def get_gmail_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = "token.json"
    creds_path = os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")
    creds = None

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_new_senders(service, hours: int = 24, processed_ids: set[str] | None = None) -> tuple[list[dict], set[str]]:
    """
    Recupera i mittenti reali unici dalle email ricevute nell'arco di `hours`.
    Restituisce (lista_mittenti, set_message_ids_processati).
    """
    if processed_ids is None:
        processed_ids = set()

    after_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"in:inbox after:{after_ts} -in:sent -in:draft"

    page_token = None
    raw_messages: list[dict] = []

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 200}
        if page_token:
            kwargs["pageToken"] = page_token

        result = service.users().messages().list(**kwargs).execute()
        raw_messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    seen_emails: set[str] = set()
    new_ids: set[str] = set()
    senders: list[dict] = []

    for ref in raw_messages:
        msg_id = ref["id"]
        if msg_id in processed_ids:
            continue

        new_ids.add(msg_id)

        msg = service.users().messages().get(
            userId="me",
            messageId=msg_id,
            format="metadata",
            metadataHeaders=["From"],
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        display_name, email = parseaddr(raw_from)

        if not email or "@" not in email:
            continue
        email = email.lower().strip()

        if email in seen_emails or is_automated(email):
            continue

        seen_emails.add(email)
        domain = email.split("@")[1]
        first, last = split_name(display_name, email)

        senders.append({
            "email": email,
            "firstname": first,
            "lastname": last,
            "company": company_from_domain(domain),
            "domain": domain,
        })

    return senders, new_ids


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------

def get_hubspot_client():
    import hubspot
    token = os.environ.get("HUBSPOT_API_KEY")
    if not token:
        raise EnvironmentError("HUBSPOT_API_KEY non impostata")
    return hubspot.Client.create(access_token=token)


def find_contact(client, email: str) -> Optional[dict]:
    from hubspot.crm.contacts.exceptions import ApiException
    try:
        resp = client.crm.contacts.basic_api.get_by_id(
            contact_id=email,
            id_property="email",
            properties=["email", "firstname", "lastname", "company"],
        )
        return {"id": resp.id, "properties": resp.properties}
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def create_contact(client, sender: dict) -> str:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate
    props = {
        "email": sender["email"],
        "hs_analytics_source_data_1": "Inbound Gmail",
    }
    if sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if sender["company"]:
        props["company"] = sender["company"]

    resp = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
    )
    return resp.id


def update_contact(client, contact_id: str, sender: dict, existing: dict) -> bool:
    from hubspot.crm.contacts import SimplePublicObjectInput
    updates: dict[str, str] = {}

    for field in ("firstname", "lastname", "company"):
        if not existing.get(field) and sender.get(field):
            updates[field] = sender[field]

    if not updates:
        return False

    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return True


# ---------------------------------------------------------------------------
# Orchestrazione principale
# ---------------------------------------------------------------------------

def run_sync(hours: int = 24) -> list[dict]:
    logger.info(f"=== Avvio sync Gmail → HubSpot (ultime {hours}h) ===")

    processed_ids = load_processed_ids()

    gmail = get_gmail_service()
    senders, new_ids = fetch_new_senders(gmail, hours=hours, processed_ids=processed_ids)
    logger.info(f"Email nuove scansionate: {len(new_ids)} | Mittenti reali unici: {len(senders)}")

    if not senders:
        logger.info("Nessun nuovo mittente da processare.")
        save_processed_ids(processed_ids | new_ids)
        return []

    client = get_hubspot_client()
    report: list[dict] = []

    for sender in senders:
        email = sender["email"]
        try:
            existing = find_contact(client, email)

            if existing is None:
                contact_id = create_contact(client, sender)
                status = "CREATO"
            else:
                contact_id = existing["id"]
                updated = update_contact(client, contact_id, sender, existing["properties"])
                status = "AGGIORNATO" if updated else "IGNORATO"

            report.append({"stato": status, "email": email, "hubspot_id": contact_id})
            logger.info(f"[{status}] {email} → ID {contact_id}")

        except Exception as exc:
            logger.error(f"Errore per {email}: {exc}")
            report.append({"stato": "ERRORE", "email": email, "hubspot_id": None, "errore": str(exc)})

    save_processed_ids(processed_ids | new_ids)
    return report


def print_report(report: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("REPORT SYNC Gmail → HubSpot")
    print("=" * 72)
    print(f"{'STATO':<12}  {'EMAIL':<42}  {'HUBSPOT ID'}")
    print("-" * 72)
    for row in report:
        print(f"{row['stato']:<12}  {row['email']:<42}  {row.get('hubspot_id') or 'N/A'}")

    totals: dict[str, int] = {}
    for row in report:
        totals[row["stato"]] = totals.get(row["stato"], 0) + 1

    print("-" * 72)
    print("Riepilogo: " + " | ".join(f"{k}: {v}" for k, v in totals.items()))
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument("--hours", type=int, default=24, help="Finestra temporale in ore (default: 24)")
    args = parser.parse_args()

    report = run_sync(hours=args.hours)
    if report:
        print_report(report)


if __name__ == "__main__":
    main()
