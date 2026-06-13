"""
Gmail → HubSpot Contact Sync
Estrae i mittenti dalle email in arrivo su Gmail e li sincronizza in HubSpot.
Usa l'email come chiave unica; crea nuovi contatti o aggiorna quelli esistenti.
"""

from __future__ import annotations

import re
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Costanti ─────────────────────────────────────────────────────────────────
CONTACT_SOURCE = "Gmail"
INBOUND_TAG    = "Inbound Gmail"
LOOKBACK_DAYS  = 7          # quanti giorni di email da controllare
PAGE_SIZE      = 50         # thread per pagina Gmail

# Email da ignorare (noreply, bounce, sistemi automatici)
IGNORE_PATTERNS = [
    r"noreply",
    r"no-reply",
    r"notification@",
    r"mailer-daemon",
    r"analytics-noreply",
    r"facebookmail\.com",
    r"googlemail\.com",
    r"@latestata\.it",           # redazione interna – già gestita
    r"cristian\.mameli\.editore", # mittente principale
    r"pubblica\.latestata",       # alias mittente
]

# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class ContactInfo:
    email:     str
    firstname: str = ""
    lastname:  str = ""
    company:   str = ""
    source:    str = CONTACT_SOURCE

    @staticmethod
    def from_sender(sender_raw: str) -> Optional["ContactInfo"]:
        """Costruisce un ContactInfo dal campo 'From' dell'email."""
        display_name, address = parseaddr(sender_raw)
        if not address or not re.match(r"[^@]+@[^@]+\.[^@]+", address):
            return None
        address = address.lower().strip()
        if _should_ignore(address):
            return None

        ci = ContactInfo(email=address)

        # Nome / cognome dal display_name
        parts = display_name.strip().split()
        if len(parts) >= 2:
            ci.firstname = parts[0]
            ci.lastname  = " ".join(parts[1:])
        elif len(parts) == 1:
            ci.firstname = parts[0]

        # Azienda dal dominio (solo se non è Gmail/Libero/hotmail…)
        domain = address.split("@")[-1]
        if not _is_personal_domain(domain):
            ci.company = _domain_to_company(domain)

        return ci


@dataclass
class SyncResult:
    email:    str
    status:   str           # "Creato" | "Aggiornato" | "Ignorato" | "Errore"
    hs_id:    Optional[int] = None
    note:     str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "libero.it", "alice.it", "virgilio.it", "tiscali.it", "live.com",
}

def _is_personal_domain(domain: str) -> bool:
    return domain in PERSONAL_DOMAINS

def _domain_to_company(domain: str) -> str:
    """Ricava un nome azienda grezzo dal dominio: rimuove TLD e trattini."""
    base = domain.rsplit(".", 1)[0]   # es. "moonsrl" da "moonsrl.it"
    return " ".join(w.capitalize() for w in re.split(r"[-_]", base))

def _should_ignore(email: str) -> bool:
    return any(re.search(p, email, re.IGNORECASE) for p in IGNORE_PATTERNS)


# ── Logica principale (da adattare ai client MCP concreti) ───────────────────

def extract_unique_senders(threads: list[dict]) -> list[ContactInfo]:
    """
    Data la risposta di mcp__Gmail__search_threads, restituisce
    ContactInfo unici per ogni mittente esterno valido.
    """
    seen: set[str] = set()
    contacts: list[ContactInfo] = []

    for thread in threads:
        for msg in thread.get("messages", []):
            # Salta messaggi SENT (inviati dall'utente)
            if "SENT" in msg.get("labelIds", []):
                continue
            sender_raw = msg.get("sender", "")
            ci = ContactInfo.from_sender(sender_raw)
            if ci and ci.email not in seen:
                seen.add(ci.email)
                contacts.append(ci)
                log.debug("Estratto contatto: %s", ci.email)

    return contacts


def sync_contact_to_hubspot(ci: ContactInfo, hs_client) -> SyncResult:
    """
    Cerca il contatto in HubSpot per email.
    • Se esiste → aggiorna i campi vuoti.
    • Se non esiste → crea nuovo contatto.
    Restituisce un SyncResult.
    """
    existing = hs_client.search_by_email(ci.email)

    if existing:
        hs_id = existing["id"]
        updates = _build_update_properties(ci, existing["properties"])
        if updates:
            hs_client.update_contact(hs_id, updates)
            return SyncResult(email=ci.email, status="Aggiornato", hs_id=hs_id,
                              note=f"Campi aggiornati: {list(updates.keys())}")
        return SyncResult(email=ci.email, status="Ignorato", hs_id=hs_id,
                          note="Nessun campo mancante")
    else:
        props = {
            "email":            ci.email,
            "hs_lead_source":   ci.source,
        }
        if ci.firstname: props["firstname"] = ci.firstname
        if ci.lastname:  props["lastname"]  = ci.lastname
        if ci.company:   props["company"]   = ci.company
        new_id = hs_client.create_contact(props)
        return SyncResult(email=ci.email, status="Creato", hs_id=new_id)


def _build_update_properties(ci: ContactInfo, existing_props: dict) -> dict:
    """Restituisce solo i campi che mancano nel contatto esistente."""
    updates = {}
    if ci.firstname and not existing_props.get("firstname"):
        updates["firstname"] = ci.firstname
    if ci.lastname and not existing_props.get("lastname"):
        updates["lastname"] = ci.lastname
    if ci.company and not existing_props.get("company"):
        updates["company"] = ci.company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = ci.source
    return updates


# ── Entry‑point ───────────────────────────────────────────────────────────────

def run_sync(gmail_threads: list[dict], hs_client) -> list[SyncResult]:
    """
    Punto d'ingresso: riceve i thread Gmail e un client HubSpot
    e restituisce la lista di SyncResult.
    """
    contacts = extract_unique_senders(gmail_threads)
    log.info("Contatti unici estratti: %d", len(contacts))

    results = []
    for ci in contacts:
        try:
            r = sync_contact_to_hubspot(ci, hs_client)
        except Exception as exc:
            r = SyncResult(email=ci.email, status="Errore", note=str(exc))
        results.append(r)
        log.info("[%s] %s (ID: %s)", r.status, r.email, r.hs_id or "—")

    # Riepilogo
    created  = sum(1 for r in results if r.status == "Creato")
    updated  = sum(1 for r in results if r.status == "Aggiornato")
    ignored  = sum(1 for r in results if r.status == "Ignorato")
    errors   = sum(1 for r in results if r.status == "Errore")
    log.info("Riepilogo: %d creati, %d aggiornati, %d ignorati, %d errori",
             created, updated, ignored, errors)
    return results


if __name__ == "__main__":
    # Esempio di utilizzo con mock – sostituire con client MCP reali
    print("Modulo gmail_hubspot_sync caricato.")
    print("Usa run_sync(gmail_threads, hs_client) per eseguire la sincronizzazione.")
