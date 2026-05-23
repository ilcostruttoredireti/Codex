#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Monitora la casella Gmail in arrivo, estrae i dati del mittente
(anche da email inoltrate) e li sincronizza come contatti HubSpot.

Uso:
    python gmail_hubspot_sync.py          # esegui una volta
    python gmail_hubspot_sync.py --daemon # loop continuo (default 300s)
    python gmail_hubspot_sync.py --daemon --interval 60

Variabili d'ambiente richieste:
    GMAIL_ACCESS_TOKEN    OAuth2 access token Gmail
    HUBSPOT_ACCESS_TOKEN  Private app token HubSpot
"""

import argparse
import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Configurazione ────────────────────────────────────────────────────────────

GMAIL_TOKEN = os.environ.get("GMAIL_ACCESS_TOKEN", "")
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "300"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "gmail_hubspot_state.json"))

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
HUBSPOT_BASE = "https://api.hubapi.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Prefissi email da ignorare (bot, noreply, ecc.)
SKIP_LOCAL_PARTS = {
    "noreply", "no-reply", "no_reply", "mailer-daemon",
    "bounce", "donotreply", "do-not-reply",
    "notifications", "alerts",
}


# ── Stato persistente ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"processed": [], "last_epoch": None}


def save_state(state: dict) -> None:
    tmp = list(set(state["processed"]))
    state["processed"] = tmp[-20_000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail API ─────────────────────────────────────────────────────────────────

def _gmail(method: str, path: str, **kwargs) -> dict:
    headers = {"Authorization": f"Bearer {GMAIL_TOKEN}"}
    resp = requests.request(method, f"{GMAIL_BASE}/{path}", headers=headers, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def list_inbox_threads(after_epoch: Optional[str] = None, max_results: int = 100) -> list[dict]:
    """Restituisce thread in INBOX, opzionalmente dopo un timestamp epoch."""
    query = "in:inbox -in:draft"
    if after_epoch:
        query += f" after:{after_epoch}"
    params = {"q": query, "maxResults": max_results}
    data = _gmail("GET", "threads", params=params)
    return data.get("threads", [])


def get_thread(thread_id: str) -> Optional[dict]:
    data = _gmail("GET", f"threads/{thread_id}", params={"format": "full"})
    messages = data.get("messages", [])
    # Usa il primo messaggio (il mittente originale)
    return messages[0] if messages else None


# ── Parsing email ─────────────────────────────────────────────────────────────

def _headers_dict(message: dict) -> dict:
    return {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""


def extract_plaintext(message: dict) -> str:
    """Estrae il corpo in testo semplice da un messaggio Gmail."""
    payload = message.get("payload", {})
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        return _decode_part(payload)

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            return _decode_part(part)
        for sub in part.get("parts", []):
            if sub.get("mimeType") == "text/plain":
                return _decode_part(sub)

    return ""


# Pattern per estrarre mittente originale da email inoltrate.
# Supporta intestazioni in italiano (Da/Oggetto) e inglese (From/Subject).
_FWD_RE = [
    # Da "Nome Cognome" email@domain.com
    re.compile(
        r'Da\s+"([^"]+)"\s+([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I
    ),
    # Da Nome Cognome <email@domain.com>
    re.compile(
        r'Da\s+(.+?)\s+<([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})>', re.I
    ),
    # Da: email@domain.com  (senza nome)
    re.compile(
        r'Da\s+([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I
    ),
    # From: "Nome" <email>  oppure  From: email
    re.compile(
        r'From:\s+"?([^"<\n]+?)"?\s+<([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})>', re.I
    ),
    re.compile(
        r'From:\s+([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', re.I
    ),
]

_FROM_HEADER_RE = re.compile(
    r'"?([^"<@\n]+?)"?\s*<?([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})>?'
)


def _parse_from_header(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Estrae (nome, email) dall'header From:."""
    m = _FROM_HEADER_RE.match(raw.strip())
    if m:
        name = m.group(1).strip().strip('"') or None
        email = m.group(2).lower()
        if name and name == email:
            name = None
        return name, email
    # Fallback: solo email
    m2 = re.search(r'([\w.+%-]+@[\w.-]+\.[a-zA-Z]{2,})', raw)
    if m2:
        return None, m2.group(1).lower()
    return None, None


def _extract_from_forward(body: str) -> tuple[Optional[str], Optional[str]]:
    """Estrae (nome, email) dal corpo di un'email inoltrata."""
    # Esamina solo le prime 30 righe (le intestazioni del forward)
    head = "\n".join(body.splitlines()[:30])
    for pattern in _FWD_RE:
        m = pattern.search(head)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                name, email = groups
                # Se il "nome" somiglia a un'email, non è un nome
                if "@" in name:
                    name = None
                return (name.strip() if name else None), email.lower()
            else:
                return None, groups[0].lower()
    return None, None


def _should_skip(email: str) -> bool:
    local = email.split("@")[0].lower()
    return local in SKIP_LOCAL_PARTS


def _split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def _company_from_domain(domain: str) -> str:
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def extract_contact(message: dict) -> Optional[dict]:
    """
    Restituisce un dict con i dati del contatto mittente.
    Gestisce sia email dirette che inoltrate.
    """
    headers = _headers_dict(message)
    subject = headers.get("subject", "")
    raw_from = headers.get("from", "")

    is_forward = bool(re.match(r"\s*(fw|fwd|i|inoltro)\s*:", subject, re.I))

    name: Optional[str] = None
    email: Optional[str] = None

    # 1. Se è un inoltro, cerca il mittente originale nel corpo
    if is_forward:
        body = extract_plaintext(message)
        name, email = _extract_from_forward(body)

    # 2. Fallback: header From del messaggio corrente
    if not email:
        name, email = _parse_from_header(raw_from)

    if not email:
        return None
    if _should_skip(email):
        return None

    domain = email.split("@")[1]
    firstname, lastname = _split_name(name) if name else ("", "")

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": _company_from_domain(domain),
        "domain": domain,
        "subject": subject,
        "date": headers.get("date", ""),
        "thread_id": message.get("threadId", ""),
    }


# ── HubSpot API ───────────────────────────────────────────────────────────────

def _hs(method: str, path: str, **kwargs) -> dict:
    headers = {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.request(method, f"{HUBSPOT_BASE}{path}", headers=headers, **kwargs)
    if resp.status_code == 204:
        return {}
    resp.raise_for_status()
    return resp.json()


def hs_find_by_email(email: str) -> Optional[dict]:
    payload = {
        "filterGroups": [{
            "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
        }],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    data = _hs("POST", "/crm/v3/objects/contacts/search", json=payload)
    results = data.get("results", [])
    return results[0] if results else None


def hs_create_contact(contact: dict) -> dict:
    props: dict = {
        "email": contact["email"],
        # EMAIL_MARKETING è il valore HubSpot standard per email inbound
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if contact.get("firstname"):
        props["firstname"] = contact["firstname"]
    if contact.get("lastname"):
        props["lastname"] = contact["lastname"]
    # Non generare azienda da domini generici (gmail, outlook, ecc.)
    domain = contact.get("domain", "")
    generic_domains = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "libero.it", "virgilio.it"}
    if contact.get("company") and domain not in generic_domains:
        props["company"] = contact["company"]

    return _hs("POST", "/crm/v3/objects/contacts", json={"properties": props})


def hs_update_contact(contact_id: str, existing: dict, new_data: dict) -> bool:
    """Aggiorna solo i campi vuoti. Restituisce True se ci sono state modifiche."""
    existing_props = existing.get("properties", {})
    updates: dict = {}

    for field in ("firstname", "lastname"):
        if new_data.get(field) and not existing_props.get(field):
            updates[field] = new_data[field]

    # Aggiorna company solo se mancante e il dominio non è generico
    domain = new_data.get("domain", "")
    generic_domains = {"gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "libero.it", "virgilio.it"}
    if (new_data.get("company") and not existing_props.get("company")
            and domain not in generic_domains):
        updates["company"] = new_data["company"]

    if not existing_props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    if not updates:
        return False

    _hs("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": updates})
    return True


def hs_add_note(contact_id: str, subject: str, date: str) -> None:
    """Aggiunge una nota timeline al contatto."""
    body = f"📧 Email ricevuta via Gmail\nOggetto: {subject}\nData: {date}"
    try:
        _hs("POST", "/engagements/v1/engagements", json={
            "engagement": {"active": True, "type": "NOTE"},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {"body": body},
        })
    except Exception as exc:
        log.debug("Nota timeline non aggiunta: %s", exc)


# ── Ciclo di sincronizzazione ─────────────────────────────────────────────────

def process_threads(
    processed_set: set,
    after_epoch: Optional[str],
) -> list[dict]:
    results: list[dict] = []

    threads = list_inbox_threads(after_epoch=after_epoch)
    new_threads = [t for t in threads if t["id"] not in processed_set]

    if not new_threads:
        log.info("Nessun nuovo thread da elaborare.")
        return results

    log.info("Thread nuovi trovati: %d", len(new_threads))

    for thread_meta in new_threads:
        tid = thread_meta["id"]
        result: dict = {"thread_id": tid, "status": "Ignorato", "email": None, "hubspot_id": None}

        try:
            message = get_thread(tid)
            if not message:
                processed_set.add(tid)
                results.append(result)
                continue

            contact = extract_contact(message)

            if not contact:
                processed_set.add(tid)
                results.append(result)
                continue

            email = contact["email"]
            result["email"] = email

            existing = hs_find_by_email(email)

            if existing:
                cid = str(existing["id"])
                changed = hs_update_contact(cid, existing, contact)
                hs_add_note(cid, contact["subject"], contact["date"])
                result["status"] = "Aggiornato"
                result["hubspot_id"] = cid
                log.info(
                    "[%s] %s → Aggiornato (ID: %s, modifiche: %s)",
                    tid, email, cid, changed,
                )
            else:
                created = hs_create_contact(contact)
                cid = str(created.get("id", ""))
                if cid:
                    hs_add_note(cid, contact["subject"], contact["date"])
                result["status"] = "Creato"
                result["hubspot_id"] = cid
                log.info("[%s] %s → Creato (ID: %s)", tid, email, cid)

        except requests.HTTPError as exc:
            log.error("[%s] HTTP error: %s", tid, exc)
            result["status"] = "Errore"
            result["error"] = str(exc)
        except Exception as exc:
            log.error("[%s] Errore: %s", tid, exc)
            result["status"] = "Errore"
            result["error"] = str(exc)
        finally:
            processed_set.add(tid)
            results.append(result)

    return results


def run_once() -> list[dict]:
    state = load_state()
    processed_set: set = set(state["processed"])
    after_epoch: Optional[str] = state.get("last_epoch")

    results = process_threads(processed_set, after_epoch)

    state["processed"] = list(processed_set)
    state["last_epoch"] = str(int(time.time()))
    save_state(state)

    return results


def print_report(results: list[dict]) -> None:
    print(f"\n{'Stato':<12}  {'Email':<45}  {'HubSpot ID'}")
    print("─" * 72)
    for r in results:
        print(f"{r['status']:<12}  {(r['email'] or '—'):<45}  {r.get('hubspot_id') or '—'}")

    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    errors = sum(1 for r in results if r["status"] == "Errore")
    print(f"\nRiepilogo → Creati: {created}  |  Aggiornati: {updated}  |  Ignorati: {ignored}  |  Errori: {errors}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincronizza i mittenti Gmail come contatti HubSpot"
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Esegui in loop continuo"
    )
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL,
        help="Secondi tra un ciclo e l'altro (solo con --daemon, default 300)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Log di debug"
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not GMAIL_TOKEN:
        log.error("GMAIL_ACCESS_TOKEN non impostato.")
        raise SystemExit(1)
    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN non impostato.")
        raise SystemExit(1)

    interval = args.interval

    if args.daemon:
        log.info("Daemon avviato. Intervallo: %ds", interval)
        while True:
            try:
                results = run_once()
                created = sum(1 for r in results if r["status"] == "Creato")
                updated = sum(1 for r in results if r["status"] == "Aggiornato")
                log.info("Ciclo completato → Creati: %d | Aggiornati: %d", created, updated)
            except Exception as exc:
                log.error("Errore nel ciclo: %s", exc)
            time.sleep(interval)
    else:
        results = run_once()
        print_report(results)


if __name__ == "__main__":
    main()
