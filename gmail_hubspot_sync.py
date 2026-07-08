"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo su Gmail ed estrae i mittenti come contatti HubSpot.
Evita duplicati usando l'email come chiave univoca.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SyncStatus(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    SKIPPED = "Saltato"


# Pattern di mittenti automatici da escludere
AUTOMATED_PATTERNS = [
    r"^no-?reply@",
    r"^noreply@",
    r"^notify[-_]",
    r"^notification@",
    r"^newsletter@",
    r"^mailer@",
    r"^nobody@",
    r"@.*noreply\.",
    r"@facebookmail\.com$",
    r"^friends@facebookmail",
    r"^store_news@",
    r"@google\.com$",
    r"^googlebase-",
    r"^googlecloud@",
    r"^CloudPlatform-",
    r"^notify-noreply@",
    r"@notification\.",
    r"@e\.feedspot\.com$",
    r"^premium@academia-mail",
    r"^messaging-digest-",
    r"^sellersupport@",
]

AUTOMATED_DOMAINS = {
    "facebookmail.com",
    "google.com",
    "notification.circle.so",
    "thomsonreuters.com",
    "email.patreon.com",
    "account.canva.com",
    "moneya.es",
    "serpapi.com",
    "skool.com",
    "academia-mail.com",
    "amazon.it",
}


@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    lead_source: str = "Gmail"

    def __post_init__(self):
        if self.email and not self.domain:
            parts = self.email.split("@")
            if len(parts) == 2:
                self.domain = parts[1]
        if not self.company and self.domain:
            self.company = self.domain.split(".")[0].capitalize()


@dataclass
class SyncResult:
    email: str
    status: SyncStatus
    hubspot_id: Optional[str] = None
    reason: str = ""


def is_automated_sender(email: str) -> bool:
    """Restituisce True se il mittente è un sistema automatico da escludere."""
    email_lower = email.lower()

    for pattern in AUTOMATED_PATTERNS:
        if re.search(pattern, email_lower):
            return True

    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    if domain in AUTOMATED_DOMAINS:
        return True

    return False


def parse_sender_name(display_name: str, email: str) -> tuple[str, str]:
    """
    Estrae firstname e lastname dal display name o dall'indirizzo email.
    Formato atteso: "Nome Cognome <email>" oppure "email"
    """
    name = display_name.strip()
    if not name or name == email:
        local = email.split("@")[0]
        parts = re.split(r"[._\-]", local)
        parts = [p.capitalize() for p in parts if p]
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        return parts[0] if parts else "", ""

    parts = name.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return parts[0] if parts else "", ""


def extract_company_from_domain(domain: str) -> str:
    """Ricava il nome azienda dal dominio email."""
    # Rimuovi TLD e sottodomini comuni
    known_free = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it"}
    if domain in known_free:
        return ""

    parts = domain.split(".")
    # Prendi la parte principale (penultima senza TLD)
    if len(parts) >= 2:
        name = parts[-2]
        # Sostituisci trattini e converti in titolo
        return name.replace("-", " ").title()
    return domain.title()


def build_contact_from_email(sender_email: str, sender_name: str = "") -> SenderContact:
    """Costruisce un oggetto SenderContact dai dati del mittente Gmail."""
    domain = sender_email.split("@")[-1] if "@" in sender_email else ""
    firstname, lastname = parse_sender_name(sender_name, sender_email)
    company = extract_company_from_domain(domain)

    return SenderContact(
        email=sender_email.lower().strip(),
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
        lead_source="Gmail",
    )


# ---------------------------------------------------------------------------
# Logica di sincronizzazione (implementata con Claude MCP nell'esecuzione live)
# ---------------------------------------------------------------------------

HUBSPOT_CONTACT_FIELDS = {
    "email": "email",
    "firstname": "firstname",
    "lastname": "lastname",
    "company": "company",
    "lead_source": "lead_source",
}

GMAIL_TAG = "Inbound Gmail"
LEAD_SOURCE_VALUE = "Gmail"


def build_hubspot_properties(contact: SenderContact) -> dict:
    """Costruisce il dict di proprietà HubSpot da creare/aggiornare."""
    props = {
        "email": contact.email,
        "lead_source": LEAD_SOURCE_VALUE,
    }
    if contact.firstname:
        props["firstname"] = contact.firstname
    if contact.lastname:
        props["lastname"] = contact.lastname
    if contact.company:
        props["company"] = contact.company
    return props


def merge_properties(existing: dict, new_props: dict) -> dict:
    """
    Restituisce solo le proprietà che aggiornano campi vuoti o null.
    Evita di sovrascrivere valori già presenti.
    """
    updates = {}
    for key, value in new_props.items():
        if key == "email":
            continue  # email è la chiave, non si aggiorna
        if not existing.get(key) and value:
            updates[key] = value
    return updates


if __name__ == "__main__":
    print("Gmail → HubSpot Sync")
    print("Questo script viene eseguito come routine scheduled via Claude Code MCP.")
    print("Usa i tool Gmail e HubSpot MCP per la sincronizzazione in tempo reale.")
    print()
    print("Logica:")
    print("  1. Cerca email in:inbox newer_than:2d")
    print("  2. Filtra mittenti automatici/noreply")
    print("  3. Per ogni mittente reale: cerca in HubSpot per email")
    print("  4. Se non esiste → crea contatto con lead_source=Gmail")
    print("  5. Se esiste → aggiorna campi vuoti (no sovrascrittura)")
    print("  6. Output: Creato / Aggiornato / Ignorato per ogni email")
