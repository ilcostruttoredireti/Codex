"""
Gmail → HubSpot Contact Sync
============================
Monitora tutte le email in arrivo su Gmail, estrae i mittenti
e li sincronizza automaticamente come contatti in HubSpot.

Funzionalità:
- Estrae mittenti dalle email in arrivo
- Estrae mittenti originali da email inoltrate (Fw:/Fwd:)
- Cerca contatti esistenti in HubSpot (chiave: email)
- Crea nuovi contatti o aggiorna quelli esistenti
- Applica il label Gmail "Inbound Gmail" ai thread processati
- Evita duplicati usando l'email come chiave unica
- Output: Stato (CREATO / AGGIORNATO / IGNORATO), Email, ID HubSpot

Dipendenze:
    pip install -r requirements.txt

Configurazione:
    Copia .env.example in .env e compila le variabili.
    Per Gmail: scarica credentials.json da Google Cloud Console.
    Per HubSpot: crea una Private App con scope contacts.

Esecuzione:
    python gmail_hubspot_sync.py              # sync una tantum
    python gmail_hubspot_sync.py --watch      # polling continuo (ogni 5 min)
    python gmail_hubspot_sync.py --days 7     # ultime 7 giorni (default: 1)
"""

import os
import re
import json
import time
import email
import base64
import logging
import argparse
import datetime
from dataclasses import dataclass, field
from typing import Optional
from email.header import decode_header

from dotenv import load_dotenv

# ── Gmail ──────────────────────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# ── HubSpot ────────────────────────────────────────────────────────────────────
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Costanti ───────────────────────────────────────────────────────────────────
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
INBOUND_LABEL_NAME = "Inbound Gmail"
CONTACT_SOURCE = "Gmail"

# Domini da ignorare (sistemi, no-reply, ecc.)
SKIP_DOMAINS = {
    "accounts.google.com",
    "noreply.github.com",
    "mailer.google.com",
    "notifications.google.com",
    "bounce.amazon.com",
}

# Prefissi da ignorare
SKIP_PREFIXES = {"no-reply", "noreply", "mailer-daemon", "postmaster", "bounce-"}

# Regex per estrarre email e nome da header "From"
EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.\w+')
FROM_RE  = re.compile(r'^(?:"?([^"<]+)"?\s+)?<?([\w.+-]+@[\w.-]+\.\w+)>?$')

# Regex per mittente originale in email inoltrate (IT/EN)
FWD_FROM_RE = re.compile(
    r'(?:Da|From):\s*(?:"?([^"<\n]+)"?\s+)?<?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,})>?',
    re.IGNORECASE | re.MULTILINE,
)

# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = field(init=False)
    thread_id: str = ""

    def __post_init__(self):
        self.email = self.email.strip().lower()
        self.domain = self.email.split("@")[-1] if "@" in self.email else ""

    @property
    def is_valid(self) -> bool:
        """Ritorna False per email di sistema da ignorare."""
        if not self.email or "@" not in self.email:
            return False
        if self.domain in SKIP_DOMAINS:
            return False
        local = self.email.split("@")[0]
        if any(local.startswith(p) for p in SKIP_PREFIXES):
            return False
        return True


@dataclass
class SyncResult:
    status: str          # CREATO | AGGIORNATO | IGNORATO | ERRORE
    email: str
    hubspot_id: Optional[str] = None
    note: str = ""

    def __str__(self):
        hs = self.hubspot_id or "-"
        note = f" ({self.note})" if self.note else ""
        return f"[{self.status}] {self.email} → HubSpot ID: {hs}{note}"


# ══════════════════════════════════════════════════════════════════════════════
# Gmail helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_gmail_service():
    """Autentica con Gmail tramite OAuth2 e ritorna il service object."""
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def decode_mime_header(value: str) -> str:
    """Decodifica header MIME (es. =?utf-8?q?...?=)."""
    parts = decode_header(value or "")
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded).strip()


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Analizza l'header From e ritorna (email, firstname, lastname).
    Esempio: 'Marco Rossi <marco@example.com>' → ('marco@example.com', 'Marco', 'Rossi')
    """
    from_header = decode_mime_header(from_header)
    m = FROM_RE.match(from_header.strip())
    if m:
        display_name = (m.group(1) or "").strip().strip('"')
        addr = (m.group(2) or "").strip().lower()
    else:
        emails = EMAIL_RE.findall(from_header)
        addr = emails[0].lower() if emails else ""
        display_name = ""

    firstname, lastname = "", ""
    if display_name:
        parts = display_name.split()
        if len(parts) >= 2:
            firstname = parts[0].strip()
            lastname = " ".join(parts[1:]).strip()
        else:
            firstname = display_name.strip()

    return addr, firstname, lastname


def extract_company_from_domain(domain: str) -> str:
    """
    Inferisce il nome azienda dal dominio email.
    es. 'nextpress.it' → 'Nextpress', 'comune.roma.it' → 'Comune Roma'
    """
    if not domain or domain.endswith("gmail.com") or domain.endswith("hotmail.it"):
        return ""
    # Rimuove TLD e normalizza
    parts = domain.split(".")
    name_parts = [p.capitalize() for p in parts[:-1]]  # esclude .it/.com/ecc.
    return " ".join(name_parts)


def get_body_text(payload: dict) -> str:
    """Estrae il testo plain dal payload del messaggio Gmail."""
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = get_body_text(part)
        if text:
            return text
    return ""


def extract_forwarded_senders(body: str, thread_id: str) -> list[SenderInfo]:
    """
    Estrae i mittenti originali da email inoltrate (Fw: / Fwd:).
    Analizza il corpo cercando pattern 'Da: Nome <email>' o 'From: email'.
    """
    senders = []
    for m in FWD_FROM_RE.finditer(body):
        display = (m.group(1) or "").strip().strip('"')
        addr = m.group(2).strip().lower()
        if not addr:
            continue
        firstname, lastname = "", ""
        if display:
            parts = display.split()
            if len(parts) >= 2:
                firstname = parts[0]
                lastname = " ".join(parts[1:])
            else:
                firstname = display
        domain = addr.split("@")[-1]
        company = extract_company_from_domain(domain)
        si = SenderInfo(
            email=addr,
            firstname=firstname,
            lastname=lastname,
            company=company,
            thread_id=thread_id,
        )
        if si.is_valid:
            senders.append(si)
    return senders


def fetch_inbox_senders(service, days: int = 1, max_results: int = 500) -> list[SenderInfo]:
    """
    Scarica i messaggi in arrivo degli ultimi N giorni e restituisce
    la lista dei mittenti unici (diretti + originali da inoltri).
    """
    after = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y/%m/%d")
    query = f"in:inbox -from:me after:{after}"
    logger.info(f"Ricerca Gmail con: {query}")

    all_senders: dict[str, SenderInfo] = {}

    page_token = None
    fetched = 0
    while fetched < max_results:
        params = {"userId": "me", "q": query, "maxResults": min(50, max_results - fetched)}
        if page_token:
            params["pageToken"] = page_token

        resp = service.users().messages().list(**params).execute()
        messages = resp.get("messages", [])
        if not messages:
            break

        for msg_stub in messages:
            msg = service.users().messages().get(
                userId="me", id=msg_stub["id"], format="full"
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_val = headers.get("From", "")
            subject  = headers.get("Subject", "")
            thread_id = msg.get("threadId", "")

            addr, firstname, lastname = parse_sender(from_val)
            domain  = addr.split("@")[-1] if "@" in addr else ""
            company = extract_company_from_domain(domain)

            si = SenderInfo(
                email=addr, firstname=firstname, lastname=lastname,
                company=company, thread_id=thread_id
            )
            if si.is_valid and addr not in all_senders:
                all_senders[addr] = si
                logger.debug(f"Mittente diretto: {addr}")

            # Estrai originali da inoltri
            is_fwd = subject.lower().startswith(("fw:", "fwd:", "fw :", "fwd :"))
            if is_fwd:
                body = get_body_text(msg.get("payload", {}))
                for fwd_si in extract_forwarded_senders(body, thread_id):
                    if fwd_si.email not in all_senders:
                        all_senders[fwd_si.email] = fwd_si
                        logger.debug(f"Mittente inoltro: {fwd_si.email}")

        fetched += len(messages)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    logger.info(f"Trovati {len(all_senders)} mittenti unici")
    return list(all_senders.values())


# ══════════════════════════════════════════════════════════════════════════════
# Gmail Labels
# ══════════════════════════════════════════════════════════════════════════════

def get_or_create_label(service, name: str) -> Optional[str]:
    """Restituisce l'ID del label Gmail, creandolo se non esiste."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    # Crea il label
    body = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
        "color": {"backgroundColor": "#4a86e8", "textColor": "#ffffff"},
    }
    new_lbl = service.users().labels().create(userId="me", body=body).execute()
    logger.info(f"Label Gmail creato: '{name}' (ID: {new_lbl['id']})")
    return new_lbl["id"]


def apply_label_to_thread(service, thread_id: str, label_id: str):
    """Applica un label all'intero thread Gmail."""
    service.users().threads().modify(
        userId="me",
        id=thread_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ══════════════════════════════════════════════════════════════════════════════
# HubSpot helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_API_KEY:
        raise ValueError("HUBSPOT_API_KEY non impostato nel file .env")
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def find_contact_by_email(hs: hubspot.Client, email_addr: str) -> Optional[dict]:
    """Cerca un contatto HubSpot per email. Ritorna il record o None."""
    from hubspot.crm.contacts import Filter, FilterGroup, PublicObjectSearchRequest
    f = Filter(property_name="email", operator="EQ", value=email_addr)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.results:
        return resp.results[0]
    return None


def build_properties(si: SenderInfo, existing: Optional[dict] = None) -> dict:
    """
    Costruisce il dizionario di proprietà da inviare a HubSpot.
    Se il contatto esiste, aggiorna solo i campi vuoti.
    """
    ex_props = existing.properties if existing else {}
    props = {}

    def fill(key: str, value: str):
        if value and not ex_props.get(key):
            props[key] = value

    fill("email",     si.email)
    fill("firstname", si.firstname)
    fill("lastname",  si.lastname)
    fill("company",   si.company)

    # Imposta sempre lead status a NEW se non già settato
    if not ex_props.get("hs_lead_status"):
        props["hs_lead_status"] = "NEW"

    return props


def sync_contact(hs: hubspot.Client, si: SenderInfo) -> SyncResult:
    """
    Sincronizza un singolo contatto su HubSpot.
    Ritorna SyncResult con stato CREATO / AGGIORNATO / IGNORATO / ERRORE.
    """
    try:
        existing = find_contact_by_email(hs, si.email)
        props = build_properties(si, existing)

        if existing:
            hs_id = existing.id
            if props:
                hs.crm.contacts.basic_api.update(
                    contact_id=hs_id,
                    simple_public_object_input=SimplePublicObjectInput(properties=props),
                )
                return SyncResult("AGGIORNATO", si.email, hs_id,
                                  f"aggiornati: {list(props.keys())}")
            else:
                return SyncResult("IGNORATO", si.email, hs_id,
                                  "nessun campo da aggiornare")
        else:
            # Crea nuovo contatto — assicurati che email sia sempre presente
            props["email"] = si.email
            new_contact = hs.crm.contacts.basic_api.create(
                simple_public_object_input=SimplePublicObjectInput(properties=props)
            )
            return SyncResult("CREATO", si.email, new_contact.id)

    except ApiException as e:
        body = json.loads(e.body) if e.body else {}
        return SyncResult("ERRORE", si.email, note=body.get("message", str(e)))
    except Exception as e:
        return SyncResult("ERRORE", si.email, note=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def run_sync(days: int = 1) -> list[SyncResult]:
    """Esegue un ciclo completo di sincronizzazione Gmail → HubSpot."""
    gmail  = get_gmail_service()
    hs     = get_hubspot_client()
    label_id = get_or_create_label(gmail, INBOUND_LABEL_NAME)

    senders = fetch_inbox_senders(gmail, days=days)
    results = []
    labeled_threads: set[str] = set()

    for si in senders:
        result = sync_contact(hs, si)
        results.append(result)
        logger.info(str(result))

        # Applica label Gmail al thread (una volta per thread)
        if si.thread_id and si.thread_id not in labeled_threads and label_id:
            try:
                apply_label_to_thread(gmail, si.thread_id, label_id)
                labeled_threads.add(si.thread_id)
            except Exception as e:
                logger.warning(f"Label non applicato al thread {si.thread_id}: {e}")

    # Stampa riepilogo
    created  = sum(1 for r in results if r.status == "CREATO")
    updated  = sum(1 for r in results if r.status == "AGGIORNATO")
    ignored  = sum(1 for r in results if r.status == "IGNORATO")
    errors   = sum(1 for r in results if r.status == "ERRORE")

    print("\n" + "═" * 60)
    print(f"  SYNC COMPLETATO — {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    print("═" * 60)
    print(f"  📧 Email processate : {len(senders)}")
    print(f"  ✅ Creati           : {created}")
    print(f"  🔄 Aggiornati       : {updated}")
    print(f"  ⏭  Ignorati         : {ignored}")
    print(f"  ❌ Errori           : {errors}")
    print("═" * 60)
    print("\nDettaglio:")
    for r in results:
        icon = {"CREATO": "✅", "AGGIORNATO": "🔄", "IGNORATO": "⏭ ", "ERRORE": "❌"}.get(r.status, "?")
        hs = r.hubspot_id or "-"
        print(f"  {icon} {r.status:<12} {r.email:<45} ID: {hs}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Sync Gmail → HubSpot Contacts")
    parser.add_argument("--days",  type=int, default=1,
                        help="Quanti giorni di email analizzare (default: 1)")
    parser.add_argument("--watch", action="store_true",
                        help="Modalità polling: ripete il sync ogni 5 minuti")
    parser.add_argument("--interval", type=int, default=300,
                        help="Intervallo polling in secondi (default: 300)")
    args = parser.parse_args()

    if args.watch:
        logger.info(f"Modalità watch attiva — polling ogni {args.interval}s")
        while True:
            try:
                run_sync(days=args.days)
            except Exception as e:
                logger.error(f"Errore nel ciclo di sync: {e}")
            logger.info(f"Prossimo sync tra {args.interval}s...")
            time.sleep(args.interval)
    else:
        run_sync(days=args.days)


if __name__ == "__main__":
    main()
