#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls Gmail inbox, extracts senders (including from forwarded emails),
and creates or updates contacts in HubSpot.

Usage:
    python gmail_hubspot_sync.py          # polling continuo
    python gmail_hubspot_sync.py --once   # singola passata e uscita
    python gmail_hubspot_sync.py --reset  # azzera lo stato e riesegue

Variabili d'ambiente richieste (vedi .env.example):
    HUBSPOT_ACCESS_TOKEN   token privato HubSpot
    GMAIL_CREDENTIALS_FILE percorso del file credentials.json OAuth2
    GMAIL_TOKEN_FILE       percorso dove salvare il token Gmail
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

# ── dipendenze esterne ─────────────────────────────────────────────────────────
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    sys.exit("Installa le dipendenze: pip install -r requirements.txt")

try:
    import hubspot
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate
    from hubspot.crm.contacts import SimplePublicObjectInput
    from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
    from hubspot.crm.contacts.exceptions import ApiException
except ImportError:
    sys.exit("Installa le dipendenze: pip install -r requirements.txt")

# ── configurazione ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDS  = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN  = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HS_TOKEN     = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE   = Path(os.getenv("STATE_FILE", "processed_emails.json"))
POLL_SECS    = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
MAX_PER_RUN  = int(os.getenv("MAX_RESULTS_PER_RUN", "100"))

# Mittenti/domini da ignorare
SKIP_SENDERS = {
    "mailer-daemon@googlemail.com",
    "noreply@google.com",
    "no-reply@accounts.google.com",
    "analytics-noreply@google.com",
    "posta-certificata@legalmail.it",
    "notification@priority.facebookmail.com",
}
SKIP_DOMAINS = {
    "googlemail.com", "facebookmail.com", "bounce.com",
    "notifications.google.com", "accounts.google.com",
    "postacert.istruzione.it", "legalmail.it",
    "pec.it", "pec.sesto-fiorentino.net",
}

# Pattern per estrarre il mittente da email forwardate
# Formato XAM/Libero:  Da "Nome Cognome" nome@dominio.it
# Formato Gmail IT:    Da: Nome Cognome <nome@dominio.it>
_RE_FWD_XAM    = re.compile(
    r'^Da\s+"([^"]+)"\s+([\w.+\-]+@[\w.\-]+\.[a-z]{2,})',
    re.MULTILINE | re.IGNORECASE,
)
_RE_FWD_GMAIL  = re.compile(
    r'^Da:\s*(?:"?([^"<\n]+?)"?\s*)?<?([\w.+\-]+@[\w.\-]+\.[a-z]{2,})>?',
    re.MULTILINE | re.IGNORECASE,
)
_RE_EMAIL_BARE = re.compile(r'[\w.+\-]+@[\w.\-]+\.[a-z]{2,}')

# ── logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── stato locale ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"processed_message_ids": [], "last_run": None, "stats": {"created": 0, "updated": 0, "skipped": 0}}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def _get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDS).exists():
                log.error(
                    "File '%s' non trovato. Scaricalo da Google Cloud Console "
                    "(API & Services → Credentials → OAuth 2.0 Client IDs).", GMAIL_CREDS
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _fetch_unprocessed(service, processed_ids: set) -> list[dict]:
    messages, page_token = [], None
    while True:
        kwargs = {"userId": "me", "labelIds": ["INBOX"], "maxResults": MAX_PER_RUN}
        if page_token:
            kwargs["pageToken"] = page_token
        res   = service.users().messages().list(**kwargs).execute()
        items = res.get("messages", [])
        for item in items:
            if item["id"] not in processed_ids:
                messages.append(item)
        page_token = res.get("nextPageToken")
        if not page_token or not items:
            break
    return messages


def _get_message_data(service, msg_id: str) -> Optional[dict]:
    """Restituisce {id, from, subject, date, body_text} o None."""
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
    except HttpError as e:
        log.warning("Impossibile leggere messaggio %s: %s", msg_id, e)
        return None

    headers = {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }

    def _get_body(payload) -> str:
        """Estrae il testo piano dal payload MIME."""
        import base64
        mime = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data", "")
        if body_data and "text/plain" in mime:
            return base64.urlsafe_b64decode(body_data + "==").decode("utf-8", errors="replace")
        for part in payload.get("parts", []):
            text = _get_body(part)
            if text:
                return text
        return ""

    return {
        "id":      msg_id,
        "from":    headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date":    headers.get("date", ""),
        "body":    _get_body(msg.get("payload", {})),
    }


# ── estrazione contatti ────────────────────────────────────────────────────────

def _parse_name_email(raw: str) -> tuple[str, str, str]:
    """(email, first_name, last_name) da una stringa mittente."""
    name, email = parseaddr(raw)
    email = email.lower().strip()
    first, last = "", ""
    if name:
        name = name.strip().strip('"')
        parts = name.split(None, 1)
        first = parts[0] if parts else ""
        last  = parts[1] if len(parts) > 1 else ""
    return email, first, last


def _extract_forwarded_sender(body: str) -> tuple[str, str, str]:
    """Cerca il mittente originale nel corpo di email forwardata."""
    m = _RE_FWD_XAM.search(body)
    if m:
        name_raw, email = m.group(1).strip(), m.group(2).lower().strip()
        parts = name_raw.split(None, 1)
        return email, parts[0] if parts else "", parts[1] if len(parts) > 1 else ""

    m = _RE_FWD_GMAIL.search(body)
    if m:
        name_raw = (m.group(1) or "").strip()
        email    = m.group(2).lower().strip()
        parts = name_raw.split(None, 1) if name_raw else []
        return email, parts[0] if parts else "", parts[1] if len(parts) > 1 else ""

    return "", "", ""


def _domain(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def _company_from_domain(email: str) -> str:
    dom = _domain(email)
    if not dom:
        return ""
    return dom.split(".")[0].replace("-", " ").replace("_", " ").title()


def _should_skip(email: str) -> bool:
    if not email or email in SKIP_SENDERS:
        return True
    return _domain(email) in SKIP_DOMAINS


_RELAY_ADDRESSES = {"redazione@latestata.it", "pubblica.latestata@gmail.com"}


def extract_contact(msg: dict) -> Optional[dict]:
    """
    Restituisce {email, first, last, company, subject, date} o None.
    Se il mittente diretto è un indirizzo relay, cerca nel corpo.
    """
    direct_email, first, last = _parse_name_email(msg["from"])

    if _should_skip(direct_email):
        return None

    # Se è un indirizzo relay/forwarding, cerca il vero mittente nel corpo
    if direct_email in _RELAY_ADDRESSES or not direct_email:
        fw_email, fw_first, fw_last = _extract_forwarded_sender(msg["body"])
        if fw_email and not _should_skip(fw_email):
            email, first, last = fw_email, fw_first, fw_last
        else:
            return None
    else:
        email = direct_email

    return {
        "email":   email,
        "first":   first,
        "last":    last,
        "company": _company_from_domain(email),
        "subject": msg["subject"],
        "date":    msg["date"],
    }


# ── HubSpot helpers ────────────────────────────────────────────────────────────

def _hs_client():
    return hubspot.Client.create(access_token=HS_TOKEN)


def _find_contact(client, email: str) -> Optional[object]:
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[
            Filter(property_name="email", operator="EQ", value=email)
        ])],
        properties=["email", "firstname", "lastname", "company"],
        limit=1,
    )
    try:
        res = client.crm.contacts.search_api.do_search(req)
        return res.results[0] if res.results else None
    except ApiException as e:
        log.error("HubSpot search error (%s): %s", email, e.status)
        return None


def _create_contact(client, c: dict) -> Optional[str]:
    props = {"email": c["email"], "hs_lead_source": "OTHER"}
    if c["first"]:   props["firstname"] = c["first"]
    if c["last"]:    props["lastname"]  = c["last"]
    if c["company"]: props["company"]   = c["company"]
    try:
        obj = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return obj.id
    except ApiException as e:
        log.error("HubSpot create error (%s): %s", c["email"], e.status)
        return None


def _update_contact(client, contact_id: str, c: dict) -> bool:
    """Aggiorna solo i campi vuoti."""
    try:
        existing = client.crm.contacts.basic_api.get_by_id(
            contact_id, properties=["firstname", "lastname", "company"]
        )
    except ApiException:
        return False

    ep = existing.properties
    updates = {}
    if not ep.get("firstname") and c["first"]:   updates["firstname"] = c["first"]
    if not ep.get("lastname")  and c["last"]:    updates["lastname"]  = c["last"]
    if not ep.get("company")   and c["company"]: updates["company"]   = c["company"]

    if not updates:
        return False

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as e:
        log.error("HubSpot update error (%s): %s", contact_id, e.status)
        return False


def _add_note(client, contact_id: str, subject: str, date: str) -> None:
    """Aggiunge una nota di attività al contatto."""
    try:
        body = f"Email inbound via Gmail\nOggetto: {subject}\nData: {date}"
        ts   = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=hubspot.crm.objects.notes.SimplePublicObjectInputForCreate(
                properties={"hs_note_body": body, "hs_timestamp": ts}
            )
        )
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as e:
        log.debug("Nota non aggiunta per %s: %s", contact_id, e)


# ── logica principale ──────────────────────────────────────────────────────────

def _process_message(gmail_svc, hs_client, msg_id: str) -> dict:
    result = {"status": "IGNORATO", "email": "", "hubspot_id": ""}

    msg_data = _get_message_data(gmail_svc, msg_id)
    if not msg_data:
        result["status"] = "ERRORE"
        return result

    contact = extract_contact(msg_data)
    if not contact:
        return result

    result["email"] = contact["email"]
    existing = _find_contact(hs_client, contact["email"])

    if existing:
        updated = _update_contact(hs_client, existing.id, contact)
        result["status"]     = "AGGIORNATO" if updated else "IGNORATO"
        result["hubspot_id"] = existing.id
    else:
        new_id = _create_contact(hs_client, contact)
        if new_id:
            result["status"]     = "CREATO"
            result["hubspot_id"] = new_id
            try:
                _add_note(hs_client, new_id, contact["subject"], contact["date"])
            except Exception:
                pass
        else:
            result["status"] = "ERRORE"

    return result


def run_sync(once: bool = False) -> None:
    if not HS_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN non impostato.")
        sys.exit(1)

    log.info("Inizializzazione Gmail...")
    gmail_svc = _get_gmail_service()

    log.info("Inizializzazione HubSpot...")
    hs = _hs_client()

    log.info("Gmail → HubSpot Sync avviato%s", " (--once)" if once else f" (polling ogni {POLL_SECS}s)")

    while True:
        state      = _load_state()
        processed  = set(state.get("processed_message_ids", []))
        stats      = state.setdefault("stats", {"created": 0, "updated": 0, "skipped": 0})
        run_results = []

        log.info("Scansione Gmail inbox...")
        messages = _fetch_unprocessed(gmail_svc, processed)
        log.info("  %d nuovi messaggi da processare", len(messages))

        for msg in messages:
            r = _process_message(gmail_svc, hs, msg["id"])
            processed.add(msg["id"])
            run_results.append(r)

            if r["status"] not in ("IGNORATO",):
                log.info("  %-12s  %-42s  %s", r["status"], r["email"], r["hubspot_id"])

            if   r["status"] == "CREATO":    stats["created"]  += 1
            elif r["status"] == "AGGIORNATO": stats["updated"] += 1
            else:                             stats["skipped"]  += 1

        state["processed_message_ids"] = list(processed)
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)

        creati    = sum(1 for r in run_results if r["status"] == "CREATO")
        aggiornati= sum(1 for r in run_results if r["status"] == "AGGIORNATO")
        ignorati  = sum(1 for r in run_results if r["status"] == "IGNORATO")
        log.info(
            "Passata completata: %d creati, %d aggiornati, %d ignorati  "
            "(totale sessione: %d / %d / %d)",
            creati, aggiornati, ignorati,
            stats["created"], stats["updated"], stats["skipped"],
        )

        if once:
            break

        log.info("Prossima scansione tra %d secondi...", POLL_SECS)
        time.sleep(POLL_SECS)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once",  action="store_true", help="Esegui una sola passata e termina")
    parser.add_argument("--reset", action="store_true", help="Azzera lo stato (rielabora tutti i messaggi)")
    args = parser.parse_args()

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        log.info("Stato azzerato.")

    run_sync(once=args.once)


if __name__ == "__main__":
    main()
