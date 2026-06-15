#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Monitora la casella Gmail in arrivo, estrae i mittenti e li sincronizza
automaticamente come contatti HubSpot.

Requisiti:
    pip install google-auth-oauthlib google-api-python-client hubspot-api-client python-dotenv

Variabili d'ambiente richieste (file .env o sistema):
    HUBSPOT_API_KEY      - HubSpot private-app token
    GMAIL_CREDENTIALS    - path al file credentials.json OAuth2 di Google
    GMAIL_TOKEN          - path al file token.json (generato al primo avvio)
    GMAIL_LABEL_SYNCED   - nome della label Gmail per email già processate (default: HubSpot-Synced)
    DAYS_BACK            - quanti giorni indietro cercare alla prima esecuzione (default: 7)

Uso:
    python gmail_hubspot_sync.py           # esecuzione singola
    python gmail_hubspot_sync.py --watch   # loop continuo (ogni 15 min)
"""

import os
import json
import time
import re
import argparse
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

# ── Google / Gmail ─────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── HubSpot ────────────────────────────────────────────────────────────────────
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

# ── Config ─────────────────────────────────────────────────────────────────────
GMAIL_SCOPES       = ["https://www.googleapis.com/auth/gmail.modify"]
CREDENTIALS_FILE   = os.getenv("GMAIL_CREDENTIALS", "credentials.json")
TOKEN_FILE         = os.getenv("GMAIL_TOKEN", "token.json")
HUBSPOT_API_KEY    = os.getenv("HUBSPOT_API_KEY", "")
GMAIL_LABEL_SYNCED = os.getenv("GMAIL_LABEL_SYNCED", "HubSpot-Synced")
STATE_FILE         = ".sync_state.json"
DAYS_BACK          = int(os.getenv("DAYS_BACK", "7"))
WATCH_INTERVAL_SEC = 15 * 60  # 15 minuti

# Mittenti da ignorare (automatici, interni)
SKIP_SENDERS = {
    "mailer-daemon@googlemail.com",
    "noreply@accounts.google.com",
    "notification@facebookmail.com",
    "notification@priority.facebookmail.com",
    "no-reply@google.com",
}
SKIP_DOMAINS = {"facebookmail.com", "googlemail.com", "bounce."}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, label_name: str) -> str:
    """Restituisce l'ID della label, creandola se non esiste."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == label_name:
            return lbl["id"]
    result = service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    log.info(f"Label Gmail creata: '{label_name}' (id={result['id']})")
    return result["id"]


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    since = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime("%Y/%m/%d")
    return {"last_query": f"newer:{since}", "processed_thread_ids": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch_inbox_threads(service, query: str, label_id_skip: str) -> list[dict]:
    """Ritorna i thread inbox non già etichettati come processati."""
    results = service.users().threads().list(
        userId="me",
        q=f"in:inbox {query} -label:{GMAIL_LABEL_SYNCED}",
        maxResults=100,
    ).execute()
    return results.get("threads", [])


def get_thread_sender(service, thread_id: str) -> tuple[str, str] | None:
    """
    Restituisce (email_mittente, nome_mittente) dal primo messaggio del thread
    che non sia un messaggio inviato da noi stessi.
    """
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "To"],
    ).execute()

    me_info = service.users().getProfile(userId="me").execute()
    my_email = me_info["emailAddress"].lower()

    for msg in thread.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("from", "")
        if not raw_from:
            continue

        # Parsing "Nome <email>" o solo "email"
        match = re.match(r'^(?:"?([^"<]+)"?\s+)?<?([^>]+@[^>]+)>?$', raw_from.strip())
        if not match:
            continue
        name_part = (match.group(1) or "").strip().strip('"')
        email_part = match.group(2).strip().lower()

        if email_part == my_email:
            continue
        if email_part in SKIP_SENDERS:
            return None
        if any(d in email_part for d in SKIP_DOMAINS):
            return None

        return email_part, name_part

    return None


# ── HubSpot helpers ────────────────────────────────────────────────────────────

def get_hubspot_client() -> HubSpot:
    if not HUBSPOT_API_KEY:
        raise RuntimeError("HUBSPOT_API_KEY non impostata")
    return HubSpot(access_token=HUBSPOT_API_KEY)


def find_contact_by_email(hs: HubSpot, email: str) -> dict | None:
    filt = Filter(property_name="email", operator="EQ", value=email)
    fg   = FilterGroup(filters=[filt])
    req  = PublicObjectSearchRequest(filter_groups=[fg], properties=["email", "firstname", "lastname", "company", "hs_analytics_source"])
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.total > 0:
        return resp.results[0]
    return None


def parse_name(raw_name: str) -> tuple[str, str]:
    """Divide nome e cognome (best-effort)."""
    parts = raw_name.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return raw_name.strip(), ""


def company_from_domain(email: str) -> str:
    domain = email.split("@")[-1]
    free_domains = {"gmail.com", "yahoo.com", "hotmail.com", "libero.it", "alice.it", "outlook.com", "icloud.com"}
    if domain in free_domains:
        return ""
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").title()
    return domain


def sync_contact(hs: HubSpot, email: str, raw_name: str) -> tuple[str, str]:
    """
    Crea o aggiorna il contatto HubSpot.
    Restituisce (status, hubspot_id) dove status è 'Creato' | 'Aggiornato' | 'Ignorato'.
    """
    existing = find_contact_by_email(hs, email)
    firstname, lastname = parse_name(raw_name) if raw_name else ("", "")
    company = company_from_domain(email)

    props = {
        "email": email,
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if firstname and not (existing and existing.properties.get("firstname")):
        props["firstname"] = firstname
    if lastname and not (existing and existing.properties.get("lastname")):
        props["lastname"] = lastname
    if company and not (existing and existing.properties.get("company")):
        props["company"] = company

    if existing:
        hs_id = existing.id
        updateable = {k: v for k, v in props.items() if k != "email"}
        if updateable:
            hs.crm.contacts.basic_api.update(
                contact_id=hs_id,
                simple_public_object_input={"properties": updateable},
            )
            return "Aggiornato", hs_id
        return "Ignorato", hs_id
    else:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        return "Creato", result.id


# ── Main sync loop ──────────────────────────────────────────────────────────────

def run_sync():
    gmail  = get_gmail_service()
    hs     = get_hubspot_client()
    state  = load_state()
    label_id = get_or_create_label(gmail, GMAIL_LABEL_SYNCED)

    threads = fetch_inbox_threads(gmail, state.get("last_query", ""), label_id)
    log.info(f"Thread da processare: {len(threads)}")

    results = []
    processed = set(state.get("processed_thread_ids", []))

    for thread in threads:
        tid = thread["id"]
        if tid in processed:
            continue

        sender = get_thread_sender(gmail, tid)
        if not sender:
            gmail.users().threads().modify(
                userId="me", id=tid, body={"addLabelIds": [label_id]}
            ).execute()
            processed.add(tid)
            continue

        email, name = sender
        try:
            status, hs_id = sync_contact(hs, email, name)
        except ApiException as exc:
            log.warning(f"HubSpot API error per {email}: {exc}")
            status, hs_id = "Errore", "N/A"

        # Applica label Gmail per non riprocessare
        gmail.users().threads().modify(
            userId="me", id=tid, body={"addLabelIds": [label_id]}
        ).execute()
        processed.add(tid)

        row = {"stato": status, "email": email, "hubspot_id": str(hs_id)}
        results.append(row)
        log.info(f"[{status}] {email} → HubSpot ID {hs_id}")

    # Salva stato con timestamp per prossima query
    state["last_query"] = f"newer:{datetime.now(timezone.utc).strftime('%Y/%m/%d')}"
    state["processed_thread_ids"] = list(processed)[-500:]  # mantieni ultimi 500
    save_state(state)

    return results


def print_report(results: list[dict]):
    print("\n" + "=" * 60)
    print(f"{'STATO':<12} {'EMAIL':<40} {'HUBSPOT ID'}")
    print("-" * 60)
    totals = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        print(f"{r['stato']:<12} {r['email']:<40} {r['hubspot_id']}")
        totals[r.get("stato", "Errore")] = totals.get(r.get("stato", "Errore"), 0) + 1
    print("-" * 60)
    print(f"Creati: {totals['Creato']}  Aggiornati: {totals['Aggiornato']}  "
          f"Ignorati: {totals['Ignorato']}  Errori: {totals['Errore']}")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--watch", action="store_true", help="Loop continuo ogni 15 minuti")
    args = parser.parse_args()

    if args.watch:
        log.info("Modalità watch attiva — sync ogni 15 minuti")
        while True:
            try:
                results = run_sync()
                print_report(results)
            except Exception as exc:
                log.error(f"Errore durante sync: {exc}", exc_info=True)
            log.info(f"Prossima esecuzione tra {WATCH_INTERVAL_SEC // 60} minuti...")
            time.sleep(WATCH_INTERVAL_SEC)
    else:
        results = run_sync()
        print_report(results)


if __name__ == "__main__":
    main()
