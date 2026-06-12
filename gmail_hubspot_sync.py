"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed estrae i mittenti,
creando o aggiornando i contatti in HubSpot senza duplicati.

Dipendenze MCP: Gmail, HubSpot
Esecuzione: lanciato manualmente o tramite scheduler (cron / GitHub Actions)

Stato salvato in: .sync_state.json (ultimo timestamp processato)
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path(__file__).parent / ".sync_state.json"

# Email dell'account e indirizzi da ignorare (proprietario, no-reply, ecc.)
SKIP_EMAILS = {
    "pubblica.latestata@gmail.com",
    "cristian.mameli.editore@gmail.com",
    "no-reply@accounts.google.com",
    "noreply@google.com",
}

# Domini generici che non identificano un'azienda
GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "libero.it", "tiscali.it"}

LEAD_SOURCE = "Gmail"
TAG_NOTE = "Inbound Gmail"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_sender(raw: str) -> dict:
    """
    Estrae email e nome da stringhe tipo:
      'Mario Rossi <mario@example.com>'
      'mario@example.com'
    """
    match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>', raw.strip())
    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        name = ""
        email = raw.strip().lower()

    firstname, lastname = split_name(name)
    domain = email.split("@")[-1] if "@" in email else ""
    company = domain_to_company(domain) if domain not in GENERIC_DOMAINS else ""

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def split_name(name: str) -> tuple[str, str]:
    """Divide il nome completo in firstname e lastname."""
    parts = name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def domain_to_company(domain: str) -> str:
    """
    Deriva un nome azienda leggibile dal dominio.
    Es. comune.sanseverinomarche.mc.it → Comune Sanseverinomarche
        gallerianazionalemarche.it      → Gallerianazionalemarche
    """
    if not domain:
        return ""
    root = domain.split(".")[0]
    # Togli prefissi comuni
    for prefix in ("www", "mail", "info", "press", "media", "ufficio"):
        if root == prefix and len(domain.split(".")) > 1:
            root = domain.split(".")[1]
    return root.replace("-", " ").title()


# ---------------------------------------------------------------------------
# Stato di sincronizzazione
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_sync_ts": None, "processed_thread_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Logica di sincronizzazione (chiamate MCP esternalizzate per unit-test)
# ---------------------------------------------------------------------------

def build_gmail_query(last_sync_ts: str | None) -> str:
    """Costruisce la query Gmail per le email ricevute dall'ultimo sync."""
    base = "in:inbox -from:me -is:draft"
    if last_sync_ts:
        # Gmail accetta after:YYYY/MM/DD
        date_str = last_sync_ts[:10].replace("-", "/")
        return f"{base} after:{date_str}"
    return base


def extract_contacts_from_threads(threads: list) -> list[dict]:
    """Raccoglie i mittenti unici da una lista di thread Gmail."""
    seen = set()
    contacts = []
    for thread in threads:
        for msg in thread.get("messages", []):
            sender_raw = msg.get("sender", "")
            if not sender_raw:
                continue
            contact = parse_sender(sender_raw)
            email = contact["email"]
            if email in SKIP_EMAILS or not email or email in seen:
                continue
            seen.add(email)
            contacts.append(contact)
    return contacts


def check_hubspot_contact(email: str, hs_results: list) -> dict | None:
    """Cerca un contatto nella risposta HubSpot già recuperata."""
    for r in hs_results:
        if r.get("properties", {}).get("email", "").lower() == email.lower():
            return r
    return None


def build_hubspot_properties(contact: dict, existing: dict | None) -> dict:
    """
    Costruisce il dict di proprietà HubSpot da impostare.
    Imposta solo i campi mancanti sull'esistente.
    """
    props = {}
    existing_props = existing["properties"] if existing else {}

    for field in ("firstname", "lastname", "company"):
        new_val = contact.get(field, "").strip()
        old_val = (existing_props.get(field) or "").strip()
        if new_val and not old_val:
            props[field] = new_val

    if not existing_props.get("hs_lead_source"):
        props["hs_lead_source"] = LEAD_SOURCE

    return props


# ---------------------------------------------------------------------------
# Runner principale (orchestrato dall'agente MCP)
# ---------------------------------------------------------------------------

def run_sync_report(threads: list, hs_lookup: dict) -> list[dict]:
    """
    Riceve:
      - threads: lista di thread Gmail (come da search_threads)
      - hs_lookup: dict {email -> record HubSpot o None}

    Restituisce la lista di azioni da eseguire:
      [{"action": "create"|"update"|"skip", "email": ..., "contact_id": ..., "props": ...}]
    """
    contacts = extract_contacts_from_threads(threads)
    actions = []

    for contact in contacts:
        email = contact["email"]
        existing = hs_lookup.get(email)
        props = build_hubspot_properties(contact, existing)

        if existing is None:
            actions.append({
                "action": "create",
                "email": email,
                "contact_id": None,
                "props": {
                    "email": email,
                    **{k: v for k, v in contact.items() if k != "email" and v},
                    "hs_lead_source": LEAD_SOURCE,
                },
            })
        elif props:
            actions.append({
                "action": "update",
                "email": email,
                "contact_id": existing["id"],
                "props": props,
            })
        else:
            actions.append({
                "action": "skip",
                "email": email,
                "contact_id": existing["id"],
                "props": {},
            })

    return actions


# ---------------------------------------------------------------------------
# Entry point (esecuzione diretta)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Gmail → HubSpot Sync")
    print(f"Avviato: {datetime.now(timezone.utc).isoformat()}")
    print()
    print("Questo script è progettato per essere eseguito tramite l'agente MCP.")
    print("Le chiamate a Gmail e HubSpot vengono gestite dall'agente.")
    print()
    print("Flusso:")
    print("  1. Legge lo stato da .sync_state.json")
    print("  2. Cerca email in arrivo (Gmail MCP: search_threads)")
    print("  3. Per ogni mittente unico:")
    print("     a. Cerca il contatto in HubSpot (HubSpot MCP: search_crm_objects)")
    print("     b. Crea (manage_crm_objects createRequest) o")
    print("        Aggiorna (manage_crm_objects updateRequest) il contatto")
    print("  4. Salva il nuovo timestamp in .sync_state.json")
