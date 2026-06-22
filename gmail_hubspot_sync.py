"""
Gmail → HubSpot Contact Sync
=============================
Monitora Gmail per nuove email in arrivo ed esegue l'upsert dei mittenti
su HubSpot, evitando duplicati.

Uso come script diretto (API keys richieste):
    python gmail_hubspot_sync.py

Uso come routine Claude Agent SDK: eseguito automaticamente dal prompt
nel file CLAUDE.md tramite gli strumenti MCP Gmail e HubSpot.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------
STATE_FILE = Path("state/last_sync.json")
GMAIL_QUERY = "in:inbox newer_than:1d -from:me"
CONTACT_SOURCE_TAG = "Inbound Gmail"

# Mittenti da ignorare sempre (notifiche automatiche, non persone reali)
IGNORED_SENDER_DOMAINS = {
    "facebookmail.com",
    "twitter.com",
    "notifications.google.com",
    "accounts.google.com",
    "mailer-daemon.googlemail.com",
    "mail.instagram.com",
    "linkedinmail.com",
}

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def parse_sender(raw: str) -> dict:
    """Estrae nome ed email da un header From: come 'Nome <email@domain.com>'."""
    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>$', raw.strip())
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip().lower()
    else:
        email = raw.strip().lower()
        name = ""
    domain = email.split("@")[-1] if "@" in email else ""
    return {"name": name, "email": email, "domain": domain}


def extract_name_parts(full_name: str) -> tuple[str, str]:
    """Divide un nome completo in (firstname, lastname)."""
    parts = full_name.strip().split(maxsplit=1)
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""
    return firstname, lastname


def company_from_domain(domain: str) -> str:
    """Ricava un nome azienda approssimativo dal dominio (es. latestata.it → Latestata)."""
    if not domain or domain.endswith("gmail.com") or domain.endswith("hotmail.com"):
        return ""
    name = domain.split(".")[0]
    return name.capitalize()


def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"processed_thread_ids": [], "last_sync": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


def is_ignored(email: str) -> bool:
    domain = email.split("@")[-1] if "@" in email else ""
    return domain in IGNORED_SENDER_DOMAINS


# ---------------------------------------------------------------------------
# Logica principale (pseudo-codice per esecuzione Claude Agent)
# ---------------------------------------------------------------------------
# Quando eseguito dall'agente tramite MCP tools, seguire questi step:
#
# 1. Caricare lo stato da state/last_sync.json
# 2. mcp__Gmail__search_threads(query=GMAIL_QUERY, pageSize=50)
# 3. Per ogni thread non presente in processed_thread_ids:
#    a. Estrarre sender da messages[0].sender
#    b. Skippare se is_ignored(email) o se è l'email dell'utente stesso
#    c. mcp__HubSpot__search_crm_objects(contacts, filter email=EQ sender)
#    d. Se non trovato → mcp__HubSpot__manage_crm_objects(createRequest)
#    e. Se trovato con campi mancanti → mcp__HubSpot__manage_crm_objects(updateRequest)
#    f. Creare nota attività → manage_crm_objects(notes createRequest)
#    g. Aggiungere thread_id a processed_thread_ids
# 4. Salvare stato aggiornato
# 5. Restituire report con: Stato | Email | ID HubSpot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Script progettato per esecuzione tramite Claude Agent SDK.")
    print("Per esecuzione diretta via API, integrare le credenziali Gmail e HubSpot.")
    state = load_state()
    print(f"Ultimo sync: {state.get('last_sync', 'mai')}")
    print(f"Thread già processati: {len(state.get('processed_thread_ids', []))}")
