"""
Gmail → HubSpot Contact Sync
Monitora la inbox Gmail, estrae i mittenti e li sincronizza in HubSpot.
Gestisce email dirette e messaggi inoltrati (Fwd).
"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Indirizzi da ignorare (account propri)
OWN_EMAILS = {
    "cristian.mameli.editore@gmail.com",
    "pubblica.latestata@gmail.com",
    "redazione@latestata.it",
}

# Pattern per estrarre mittente da email inoltrate (Fwd/Fw)
FWD_SENDER_PATTERN = re.compile(
    r"(?:Da|From):\s*(?P<name>[^<\n]+?)\s*[<\(](?P<email>[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})[>\)]",
    re.IGNORECASE,
)

# Mapping domini → nome azienda (override manuale)
DOMAIN_COMPANY_MAP = {
    "rec-media.it": "RECmedia",
    "ateneoveneto.org": "Ateneo Veneto",
    "gallerianazionalemarche.it": "Galleria Nazionale delle Marche",
}


@dataclass
class Contact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source: str = "Gmail"
    thread_id: str = ""
    status: str = ""
    hubspot_id: Optional[int] = None


def extract_domain(email: str) -> str:
    return email.split("@", 1)[-1].lower() if "@" in email else ""


def company_from_domain(domain: str) -> str:
    if domain in DOMAIN_COMPANY_MAP:
        return DOMAIN_COMPANY_MAP[domain]
    if domain in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com"):
        return ""
    # Capitalizza la prima parte del dominio
    return domain.split(".")[0].capitalize()


def parse_name(raw_name: str) -> tuple[str, str]:
    """Restituisce (firstname, lastname) da una stringa nome."""
    parts = raw_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def extract_fwd_sender(body: str) -> Optional[tuple[str, str]]:
    """
    Cerca il primo mittente originale in un corpo di email inoltrata.
    Restituisce (name, email) oppure None.
    """
    match = FWD_SENDER_PATTERN.search(body)
    if match:
        return match.group("name").strip(), match.group("email").strip()
    return None


def build_contact_from_message(msg: dict, thread_id: str) -> Optional[Contact]:
    """
    Costruisce un oggetto Contact da un messaggio Gmail.
    Gestisce sia email dirette sia email inoltrate.
    """
    sender_raw = msg.get("sender", "")
    subject = msg.get("subject", "")
    body = msg.get("plaintextBody", "")
    labels = msg.get("labelIds", [])

    # Estrai email e nome dal campo sender
    m = re.match(r"^(?P<name>.+?)\s*<(?P<email>[^>]+)>$", sender_raw.strip())
    if m:
        sender_email = m.group("email").strip().lower()
        sender_name = m.group("name").strip()
    else:
        sender_email = sender_raw.strip().lower()
        sender_name = ""

    # Salta email proprie come mittente diretto
    if sender_email in OWN_EMAILS:
        # Controlla se è un'email inoltrata e cerca il mittente originale
        if subject.lower().startswith(("fwd:", "fw:")):
            fwd = extract_fwd_sender(body)
            if fwd:
                sender_name, sender_email = fwd
                sender_email = sender_email.lower()
                if sender_email in OWN_EMAILS:
                    return None
            else:
                return None
        else:
            return None

    # Non processare email SENT (non inbox)
    if "SENT" in labels and "INBOX" not in labels:
        return None

    firstname, lastname = parse_name(sender_name) if sender_name else ("", "")
    domain = extract_domain(sender_email)
    company = company_from_domain(domain)

    return Contact(
        email=sender_email,
        firstname=firstname,
        lastname=lastname,
        company=company,
        thread_id=thread_id,
    )


def sync_contact_to_hubspot(contact: Contact, hs_results: list[dict]) -> Contact:
    """
    Logica di sync: aggiorna i dati e imposta lo status.
    Questa funzione lavora sui dati già recuperati da HubSpot.
    """
    if not hs_results:
        contact.status = "CREATO"
        return contact

    existing = hs_results[0]
    contact.hubspot_id = int(existing["id"])
    props = existing.get("properties", {})

    # Aggiorna solo i campi mancanti
    updates = {}
    if not props.get("company") and contact.company:
        updates["company"] = contact.company
    if not props.get("firstname") and contact.firstname:
        updates["firstname"] = contact.firstname
    if not props.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname

    contact.status = "AGGIORNATO"
    return contact


def process_results(contacts: list[Contact]) -> list[dict]:
    """Formatta i risultati per il log e la notifica."""
    output = []
    for c in contacts:
        output.append({
            "stato": c.status,
            "email": c.email,
            "hubspot_id": c.hubspot_id,
            "nome": f"{c.firstname} {c.lastname}".strip(),
            "azienda": c.company,
        })
    return output


def format_report(results: list[dict]) -> str:
    lines = ["=== Gmail → HubSpot Sync Report ===", ""]
    created = [r for r in results if r["stato"] == "CREATO"]
    updated = [r for r in results if r["stato"] == "AGGIORNATO"]
    skipped = [r for r in results if r["stato"] == "IGNORATO"]

    for group, label in [(created, "CREATI"), (updated, "AGGIORNATI"), (skipped, "IGNORATI")]:
        if group:
            lines.append(f"--- {label} ({len(group)}) ---")
            for r in group:
                hs = f"HubSpot ID: {r['hubspot_id']}" if r["hubspot_id"] else "nuovo"
                name_part = f" ({r['nome']})" if r["nome"] else ""
                co_part = f" [{r['azienda']}]" if r["azienda"] else ""
                lines.append(f"  {r['stato']:10s} | {r['email']}{name_part}{co_part} | {hs}")
            lines.append("")

    lines.append(f"Totale processati: {len(results)} | Creati: {len(created)} | Aggiornati: {len(updated)} | Ignorati: {len(skipped)}")
    return "\n".join(lines)
