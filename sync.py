"""
Gmail → HubSpot Contact Sync
============================================================
Polls the Gmail inbox and syncs every new sender as a contact
in HubSpot, avoiding duplicates and filling in missing fields.

Usage:
    python sync.py          # continuous polling loop
    python sync.py --once   # single pass, then exit

Environment variables (copy .env.example → .env):
    HUBSPOT_TOKEN           HubSpot private-app token (required)
    GMAIL_CREDENTIALS_FILE  Path to Google OAuth credentials JSON (default: credentials.json)
    GMAIL_TOKEN_FILE        Path to cached OAuth token (default: token.json)
    GMAIL_QUERY             Gmail search query (default: in:inbox)
    GMAIL_MAX_RESULTS       Max messages per poll (default: 50)
    GMAIL_OWN_EMAIL         Your Gmail address – skips own outbound replies
    STATE_FILE              JSON file that tracks processed message IDs (default: processed_messages.json)
    POLL_INTERVAL_SECONDS   Seconds between polls (default: 300)
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox")
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "50"))
GMAIL_OWN_EMAIL = os.getenv("GMAIL_OWN_EMAIL", "").strip().lower()

HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
HUBSPOT_BASE = "https://api.hubapi.com"

STATE_FILE = os.getenv("STATE_FILE", "processed_messages.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

CONTACT_SOURCE = "Gmail"

# ── State ─────────────────────────────────────────────────────────────────────


def load_state() -> set[str]:
    p = Path(STATE_FILE)
    if p.exists():
        return set(json.loads(p.read_text()).get("processed", []))
    return set()


def save_state(processed: set[str]) -> None:
    Path(STATE_FILE).write_text(
        json.dumps({"processed": sorted(processed)}, indent=2)
    )


# ── Gmail ─────────────────────────────────────────────────────────────────────


def get_gmail_service():
    creds: Optional[Credentials] = None
    token_path = Path(GMAIL_TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _header_map(msg: dict) -> dict[str, str]:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


def extract_sender(msg: dict) -> tuple[str, str, str]:
    """Return (email_addr, display_name, domain)."""
    headers = _header_map(msg)
    raw_from = headers.get("From", "")
    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.strip().lower()
    display_name = display_name.strip().strip('"')
    domain = email_addr.split("@")[-1] if "@" in email_addr else ""
    return email_addr, display_name, domain


def fetch_new_messages(service, processed: set[str]) -> list[dict]:
    """Return full message objects for inbox messages not yet processed."""
    response = (
        service.users()
        .messages()
        .list(userId="me", q=GMAIL_QUERY, maxResults=GMAIL_MAX_RESULTS)
        .execute()
    )
    new_msgs: list[dict] = []
    for stub in response.get("messages", []):
        if stub["id"] not in processed:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=stub["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            new_msgs.append(msg)
    return new_msgs


# ── HubSpot helpers ───────────────────────────────────────────────────────────

_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "protonmail.com", "me.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
}


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def company_from_domain(domain: str) -> str:
    if not domain or domain in _GENERIC_DOMAINS:
        return ""
    name = domain.split(".")[0]
    return name.capitalize()


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


# ── HubSpot API calls ─────────────────────────────────────────────────────────


def find_contact(email: str) -> Optional[dict]:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "leadsource"],
        "limit": 1,
    }
    r = requests.post(url, json=payload, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def create_contact(email: str, name: str, domain: str) -> dict:
    first, last = split_name(name)
    props: dict[str, str] = {"email": email, "leadsource": CONTACT_SOURCE}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    company = company_from_domain(domain)
    if company:
        props["company"] = company
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        json={"properties": props},
        headers=_hs_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def update_contact(contact_id: str, name: str, domain: str, existing: dict) -> dict:
    """Fill in any empty fields on an existing contact."""
    ep = existing.get("properties", {})
    first, last = split_name(name)
    company = company_from_domain(domain)

    updates: dict[str, str] = {}
    if first and not ep.get("firstname"):
        updates["firstname"] = first
    if last and not ep.get("lastname"):
        updates["lastname"] = last
    if company and not ep.get("company"):
        updates["company"] = company
    if not ep.get("leadsource"):
        updates["leadsource"] = CONTACT_SOURCE

    if not updates:
        return {"id": contact_id, "_no_changes": True}

    r = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        json={"properties": updates},
        headers=_hs_headers(),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def log_email_note(contact_id: str, subject: str, msg_date: str) -> None:
    """Attach an inbound-email note to the contact's timeline."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    payload = {
        "properties": {
            "hs_note_body": (
                f"[Inbound Gmail] Email ricevuta il {msg_date}.\n"
                f"Oggetto: {subject}"
            ),
            "hs_timestamp": str(now_ms),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note → contact
                    }
                ],
            }
        ],
    }
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/notes",
        json=payload,
        headers=_hs_headers(),
        timeout=15,
    )
    if r.status_code not in (200, 201):
        log.warning("Nota non creata per il contatto %s: %s", contact_id, r.text[:200])


# ── Core processing ───────────────────────────────────────────────────────────


def process_message(msg: dict) -> dict:
    """
    Process a single Gmail message.

    Returns a result dict:
        status  : "CREATO" | "AGGIORNATO" | "IGNORATO"
        email   : sender email address
        id      : HubSpot contact ID (or None)
        reason  : explanation when IGNORATO
    """
    email_addr, name, domain = extract_sender(msg)
    headers = _header_map(msg)
    subject = headers.get("Subject", "(nessun oggetto)")
    msg_date = headers.get("Date", "")

    if not email_addr:
        return {"status": "IGNORATO", "email": "(sconosciuta)", "id": None,
                "reason": "header From assente"}

    if GMAIL_OWN_EMAIL and email_addr == GMAIL_OWN_EMAIL:
        return {"status": "IGNORATO", "email": email_addr, "id": None,
                "reason": "email propria (mittente = account corrente)"}

    existing = find_contact(email_addr)

    if existing:
        contact_id = existing["id"]
        result = update_contact(contact_id, name, domain, existing)
        status = "IGNORATO (nessun campo da aggiornare)" if result.get("_no_changes") else "AGGIORNATO"
    else:
        created = create_contact(email_addr, name, domain)
        contact_id = created["id"]
        status = "CREATO"

    log_email_note(contact_id, subject, msg_date)
    return {"status": status, "email": email_addr, "id": contact_id}


def run_once(service, processed: set[str]) -> list[dict]:
    new_msgs = fetch_new_messages(service, processed)
    log.info("Trovati %d nuovi messaggi.", len(new_msgs))

    results: list[dict] = []
    for msg in new_msgs:
        msg_id = msg["id"]
        try:
            res = process_message(msg)
            results.append(res)
            log.info(
                "%-35s  %-40s  ID: %s",
                res["email"],
                res["status"],
                res.get("id") or "—",
            )
        except requests.HTTPError as exc:
            log.error("Errore HTTP per messaggio %s: %s", msg_id, exc.response.text[:300])
        except Exception as exc:
            log.error("Errore per messaggio %s: %s", msg_id, exc)
        finally:
            processed.add(msg_id)

    save_state(processed)
    return results


def print_report(results: list[dict]) -> None:
    print()
    print(f"{'EMAIL':<40}  {'STATO':<42}  {'ID HUBSPOT'}")
    print("-" * 100)
    for r in results:
        print(f"{r['email']:<40}  {r['status']:<42}  {r.get('id') or '—'}")
    print()

    counts = {"CREATO": 0, "AGGIORNATO": 0, "IGNORATO": 0}
    for r in results:
        if r["status"].startswith("CREATO"):
            counts["CREATO"] += 1
        elif r["status"].startswith("AGGIORNATO"):
            counts["AGGIORNATO"] += 1
        else:
            counts["IGNORATO"] += 1

    print(
        f"Riepilogo: {counts['CREATO']} creati, "
        f"{counts['AGGIORNATO']} aggiornati, "
        f"{counts['IGNORATO']} ignorati."
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once", action="store_true",
        help="Esegui un singolo ciclo e termina (invece del loop continuo)"
    )
    args = parser.parse_args()

    if not HUBSPOT_TOKEN:
        sys.exit("Errore: variabile HUBSPOT_TOKEN non impostata.")

    service = get_gmail_service()
    processed = load_state()

    log.info(
        "Avvio sync Gmail → HubSpot | Query: '%s' | Messaggi già processati: %d",
        GMAIL_QUERY,
        len(processed),
    )

    if args.once:
        results = run_once(service, processed)
        print_report(results)
        return

    log.info("Modalità continua. Intervallo: %d secondi. Premi Ctrl+C per uscire.", POLL_INTERVAL)
    while True:
        try:
            run_once(service, processed)
        except Exception as exc:
            log.error("Errore nel ciclo: %s", exc)
        log.info("Prossima esecuzione tra %d secondi…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
