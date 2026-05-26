#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
==============================
Monitora la casella Gmail in arrivo, estrae i mittenti (inclusi quelli
nei messaggi inoltrati) e li sincronizza automaticamente in HubSpot.

Funzionalità:
  - Estrazione mittente da email dirette e da messaggi Fw/Fwd
  - Deduplicazione tramite email come chiave unica
  - Creazione contatto se non esiste
  - Aggiornamento campi mancanti se il contatto esiste già
  - Fonte contatto impostata su "Gmail"
  - Log dettagliato per ogni email processata

Dipendenze:
  pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib hubspot-api-client python-dotenv

Configurazione:
  Crea un file .env nella stessa directory con:
    HUBSPOT_ACCESS_TOKEN=pat-xx-...
    GMAIL_CREDENTIALS_PATH=credentials.json
    GMAIL_TOKEN_PATH=token.json
    POLL_INTERVAL_SECONDS=300        # ogni quanto controllare (default 5 min)
    SKIP_SENDERS=redazione@latestata.it,cristian.mameli.editore@gmail.com

  Per Gmail OAuth:
    1. Vai su https://console.cloud.google.com
    2. Crea un progetto e abilita Gmail API
    3. Crea credenziali OAuth 2.0 (Desktop app)
    4. Scarica credentials.json nella directory del progetto
"""

import os
import re
import time
import json
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr
from typing import Optional
from dotenv import load_dotenv

# ─── Google / Gmail ────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import base64

# ─── HubSpot ───────────────────────────────────────────────────────────────
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput

# ─── Configurazione ────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("gmail_hubspot_sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
SKIP_SENDERS = set(
    e.strip().lower()
    for e in os.getenv(
        "SKIP_SENDERS",
        "redazione@latestata.it,cristian.mameli.editore@gmail.com",
    ).split(",")
    if e.strip()
)

# Regex per estrarre mittenti nei messaggi inoltrati (italiano e inglese)
_FWD_PATTERNS = [
    # Gmail forward in italiano/inglese
    re.compile(
        r"Da:\s*(?P<name>[^<\n]+?)\s*<(?P<email>[^>@\s]+@[^>@\s]+)>",
        re.IGNORECASE,
    ),
    re.compile(
        r"From:\s*(?P<name>[^<\n]+?)\s*<(?P<email>[^>@\s]+@[^>@\s]+)>",
        re.IGNORECASE,
    ),
    re.compile(
        r"Da:\s*(?P<email>[^<\n@\s]+@[^<\n@\s]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"From:\s*(?P<email>[^<\n@\s]+@[^<\n@\s]+)",
        re.IGNORECASE,
    ),
]


# ══════════════════════════════════════════════════════════════════════════════
# GMAIL
# ══════════════════════════════════════════════════════════════════════════════

def get_gmail_service():
    """Autentica e restituisce il servizio Gmail."""
    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
    token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
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


def get_header(headers: list, name: str) -> str:
    """Recupera un header Gmail per nome (case-insensitive)."""
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def decode_body(payload: dict) -> str:
    """Decodifica il corpo del messaggio (plain text)."""
    body = ""
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    elif mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    break

    return body


def extract_forwarded_sender(body: str) -> Optional[tuple[str, str]]:
    """
    Analizza il corpo del messaggio cercando un blocco di inoltro.
    Restituisce (nome, email) del mittente originale, o None.
    """
    # Cerca le prime N righe dopo "---Forwarded/Inoltrato---"
    lines = body.splitlines()
    in_forward_block = False
    block_lines = []

    for line in lines:
        stripped = line.strip()
        if re.search(r"(forwarded message|messaggio inoltrato|---------- Forwarded)", stripped, re.I):
            in_forward_block = True
            block_lines = []
            continue
        if in_forward_block:
            if stripped == "" and block_lines:
                break
            block_lines.append(stripped)
            if len(block_lines) > 10:
                break

    search_text = "\n".join(block_lines) if block_lines else body[:600]

    for pattern in _FWD_PATTERNS:
        m = pattern.search(search_text)
        if m:
            email = m.group("email").strip()
            name = m.groupdict().get("name", "").strip().strip('"').strip("'")
            if "@" in email and "." in email.split("@")[1]:
                return name, email

    return None


def parse_sender(raw_from: str) -> tuple[str, str]:
    """Parsa 'Nome <email>' oppure 'email' nuda."""
    name, email = parseaddr(raw_from)
    return name.strip(), email.strip().lower()


def fetch_new_messages(service, since_minutes: int = None, since_history_id: str = None) -> list[dict]:
    """
    Recupera messaggi recenti dall'inbox.
    since_minutes: quanti minuti indietro guardare (usato al primo run)
    since_history_id: Gmail history ID per fetch incrementale
    """
    query = "in:inbox -in:draft -in:sent"
    if since_minutes:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
        epoch_sec = int(cutoff.timestamp())
        query += f" after:{epoch_sec}"

    results = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.users().messages().list(**kwargs).execute()
        messages = response.get("messages", [])
        results.extend(messages)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


def get_message_detail(service, msg_id: str) -> Optional[dict]:
    """Recupera dettagli completi di un messaggio."""
    try:
        return service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
    except HttpError as e:
        log.warning(f"Impossibile recuperare il messaggio {msg_id}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# PARSING CONTATTO
# ══════════════════════════════════════════════════════════════════════════════

def extract_company_from_domain(email: str) -> str:
    """Ricava un nome azienda dal dominio email (euristico)."""
    domain = email.split("@")[-1].lower()
    # Escludi provider comuni
    skip = {"gmail.com", "yahoo.com", "hotmail.it", "hotmail.com",
            "libero.it", "outlook.com", "icloud.com", "live.it",
            "virgilio.it", "alice.it", "tiscali.it"}
    if domain in skip:
        return ""

    # Prendi la parte prima dell'estensione più esterna
    parts = domain.split(".")
    if len(parts) >= 2:
        # es: "comune.sanseverinomarche.mc.it" → "Comune Sanseverino Marche"
        name_parts = parts[:-2] if len(parts) > 2 else parts[:1]
        return " ".join(p.capitalize() for p in name_parts)
    return domain


def split_display_name(display_name: str) -> tuple[str, str]:
    """
    Tenta di dividere un display name in (firstname, lastname).
    Gestisce casi tipo 'Linda - RECmedia', 'Ufficio Stampa', 'Marco Rossi'.
    """
    if not display_name:
        return "", ""

    # Rimuovi parti dopo ' - ' che indicano azienda
    clean = re.split(r"\s+-\s+", display_name)[0].strip()

    # Rimuovi termini generici di ruolo
    role_terms = {"ufficio stampa", "ufficio comunicazione", "comunicazione",
                  "press office", "media", "redazione"}
    if clean.lower() in role_terms:
        return clean, ""

    tokens = clean.split()
    if len(tokens) == 1:
        return tokens[0], ""
    elif len(tokens) == 2:
        return tokens[0], tokens[1]
    else:
        return tokens[0], " ".join(tokens[1:])


def build_contact_data(name: str, email: str) -> dict:
    """Costruisce il dizionario di proprietà per un contatto HubSpot."""
    firstname, lastname = split_display_name(name)
    company = extract_company_from_domain(email)

    props = {"email": email}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    return props


# ══════════════════════════════════════════════════════════════════════════════
# HUBSPOT
# ══════════════════════════════════════════════════════════════════════════════

def get_hubspot_client() -> HubSpot:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN non configurato nel file .env")
    return HubSpot(access_token=token)


def find_contact_by_email(client: HubSpot, email: str) -> Optional[dict]:
    """Cerca un contatto in HubSpot tramite email. Restituisce il contatto o None."""
    try:
        from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

        f = Filter(property_name="email", operator="EQ", value=email)
        fg = FilterGroup(filters=[f])
        req = PublicObjectSearchRequest(
            filter_groups=[fg],
            properties=["email", "firstname", "lastname", "company"],
            limit=1,
        )
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if resp.results:
            return resp.results[0]
    except ApiException as e:
        log.error(f"Errore ricerca contatto {email}: {e}")
    return None


def create_contact(client: HubSpot, props: dict) -> Optional[str]:
    """Crea un nuovo contatto HubSpot. Restituisce l'ID o None."""
    try:
        obj = SimplePublicObjectInputForCreate(properties=props)
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        return result.id
    except ApiException as e:
        log.error(f"Errore creazione contatto {props.get('email')}: {e}")
    return None


def update_contact(client: HubSpot, contact_id: str, updates: dict) -> bool:
    """Aggiorna un contatto esistente con i campi forniti."""
    try:
        obj = SimplePublicObjectInput(properties=updates)
        client.crm.contacts.basic_api.update(
            contact_id=contact_id, simple_public_object_input=obj
        )
        return True
    except ApiException as e:
        log.error(f"Errore aggiornamento contatto {contact_id}: {e}")
    return False


def compute_updates(existing: dict, new_props: dict) -> dict:
    """
    Restituisce solo i campi di new_props che mancano nell'existing.
    Non sovrascrive dati già presenti.
    """
    existing_props = existing.properties if hasattr(existing, "properties") else {}
    updates = {}
    for key, val in new_props.items():
        if key == "email":
            continue  # non aggiornare l'email
        current = existing_props.get(key, "")
        if not current and val:
            updates[key] = val
    return updates


# ══════════════════════════════════════════════════════════════════════════════
# SYNC CORE
# ══════════════════════════════════════════════════════════════════════════════

def process_message(
    msg: dict,
    hs_client: HubSpot,
    processed_emails: set,
) -> dict:
    """
    Processa un singolo messaggio Gmail e sincronizza il mittente in HubSpot.

    Ritorna un dict con:
      status: "Creato" | "Aggiornato" | "Ignorato"
      email: email del contatto
      hubspot_id: ID contatto HubSpot
      reason: motivo (opzionale)
    """
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    snippet = msg.get("snippet", "")

    # ── Estrai mittente diretto ──────────────────────────────────────────────
    raw_from = get_header(headers, "From")
    direct_name, direct_email = parse_sender(raw_from)

    # ── Determina se è un forward ────────────────────────────────────────────
    subject = get_header(headers, "Subject")
    is_forward = bool(re.match(r"^(fw:|fwd:|r:|re:)", subject.strip(), re.I))

    fwd_name, fwd_email = "", ""
    if is_forward or direct_email in SKIP_SENDERS:
        body = decode_body(payload)
        result = extract_forwarded_sender(body)
        if result:
            fwd_name, fwd_email = result

    # ── Scegli quale mittente sincronizzare ──────────────────────────────────
    if fwd_email and fwd_email not in SKIP_SENDERS:
        target_email = fwd_email
        target_name = fwd_name
        source_type = "forward"
    elif direct_email and direct_email not in SKIP_SENDERS:
        target_email = direct_email
        target_name = direct_name
        source_type = "direct"
    else:
        return {
            "status": "Ignorato",
            "email": direct_email or "(nessuna)",
            "hubspot_id": None,
            "reason": "Mittente nella lista SKIP o email non trovata",
        }

    # ── Deduplicazione sessione ──────────────────────────────────────────────
    if target_email in processed_emails:
        return {
            "status": "Ignorato",
            "email": target_email,
            "hubspot_id": None,
            "reason": "Già processato in questa sessione",
        }
    processed_emails.add(target_email)

    # ── Costruisci dati contatto ─────────────────────────────────────────────
    props = build_contact_data(target_name, target_email)

    # ── Cerca in HubSpot ─────────────────────────────────────────────────────
    existing = find_contact_by_email(hs_client, target_email)

    if existing is None:
        # CREA nuovo contatto
        hs_id = create_contact(hs_client, props)
        if hs_id:
            log.info(f"✅ CREATO  | {target_email} | HubSpot ID: {hs_id}")
            return {"status": "Creato", "email": target_email, "hubspot_id": hs_id}
        else:
            return {"status": "Ignorato", "email": target_email, "hubspot_id": None,
                    "reason": "Errore creazione"}
    else:
        # AGGIORNA se ci sono campi mancanti
        updates = compute_updates(existing, props)
        if updates:
            ok = update_contact(hs_client, existing.id, updates)
            status = "Aggiornato" if ok else "Ignorato"
            log.info(f"🔄 AGGIORNATO | {target_email} | ID: {existing.id} | Campi: {list(updates.keys())}")
            return {"status": status, "email": target_email, "hubspot_id": existing.id}
        else:
            log.info(f"⏭️  IGNORATO  | {target_email} | ID: {existing.id} (nessun aggiornamento necessario)")
            return {"status": "Ignorato", "email": target_email, "hubspot_id": existing.id,
                    "reason": "Contatto già completo"}


def run_sync(since_minutes: int = 60):
    """Esegue una singola passata di sincronizzazione."""
    log.info(f"{'═' * 60}")
    log.info(f"Avvio sync Gmail → HubSpot | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Analisi email degli ultimi {since_minutes} minuti")

    try:
        gmail = get_gmail_service()
        hs = get_hubspot_client()
    except Exception as e:
        log.error(f"Errore autenticazione: {e}")
        return

    messages = fetch_new_messages(gmail, since_minutes=since_minutes)
    log.info(f"Trovati {len(messages)} messaggi da analizzare")

    processed_emails: set = set()
    results = {"Creato": [], "Aggiornato": [], "Ignorato": []}

    for msg_ref in messages:
        msg = get_message_detail(gmail, msg_ref["id"])
        if not msg:
            continue
        result = process_message(msg, hs, processed_emails)
        results[result["status"]].append(result)

    # ── Report finale ────────────────────────────────────────────────────────
    log.info(f"\n{'─' * 60}")
    log.info(f"📊 REPORT SYNC:")
    log.info(f"   ✅ Creati:     {len(results['Creato'])}")
    log.info(f"   🔄 Aggiornati: {len(results['Aggiornato'])}")
    log.info(f"   ⏭️  Ignorati:   {len(results['Ignorato'])}")
    log.info(f"{'─' * 60}\n")

    for r in results["Creato"] + results["Aggiornato"]:
        log.info(f"  [{r['status']:10}] {r['email']:<45} → ID: {r['hubspot_id']}")

    return results


def run_continuous():
    """Loop continuo: controlla ogni POLL_INTERVAL secondi."""
    log.info("🚀 Gmail → HubSpot Sync avviato in modalità continua")
    log.info(f"   Intervallo polling: {POLL_INTERVAL}s ({POLL_INTERVAL // 60} min)")
    log.info(f"   Mittenti ignorati: {', '.join(SKIP_SENDERS)}")

    first_run = True
    while True:
        since = 10080 if first_run else (POLL_INTERVAL // 60) + 2  # prima run: 7 giorni
        run_sync(since_minutes=since)
        first_run = False
        log.info(f"💤 Prossima verifica tra {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        # Singola esecuzione (es: da cron)
        minutes = 1440  # ultime 24 ore
        for arg in sys.argv:
            if arg.startswith("--since="):
                minutes = int(arg.split("=")[1])
        run_sync(since_minutes=minutes)
    else:
        # Loop continuo
        run_continuous()
