"""
Gmail → HubSpot Contact Sync
Routine automatica: monitora le email in arrivo su Gmail e sincronizza i mittenti
come contatti in HubSpot (crea nuovo o aggiorna esistente, evitando duplicati).

Eseguito come sessione schedulata Claude Code.
"""

import re
from dataclasses import dataclass
from typing import Optional

# ─── Configurazione ────────────────────────────────────────────────────────────

GMAIL_QUERY = "in:inbox -from:me newer_than:1d"
CONTACT_SOURCE_TAG = "Inbound Gmail"
NOTE_TAG = "Inbound Gmail"

# Mittenti automatici da ignorare (noreply, notifiche, mailing system)
SKIP_PATTERNS = [
    r"^notification@",
    r"^noreply@",
    r"^no-reply@",
    r"^mailer-daemon@",
    r"@.*facebookmail\.com$",
    r"@.*amazonses\.com$",
    r"@.*sendgrid\.net$",
    r"@.*mailchimp\.com$",
    r"@.*bounce\.",
    r"^postmaster@",
    r"^donotreply@",
]


# ─── Modelli dati ──────────────────────────────────────────────────────────────

@dataclass
class EmailSender:
    email: str
    display_name: Optional[str]
    domain: str
    company_guess: Optional[str]
    thread_ids: list[str]
    latest_date: str

    @property
    def firstname(self) -> Optional[str]:
        if self.display_name:
            parts = self.display_name.strip().split()
            return parts[0] if parts else None
        return None

    @property
    def lastname(self) -> Optional[str]:
        if self.display_name:
            parts = self.display_name.strip().split()
            return " ".join(parts[1:]) if len(parts) > 1 else None
        return None


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: Optional[int]
    reason: Optional[str] = None


# ─── Logica core ───────────────────────────────────────────────────────────────

def is_automated_sender(email: str) -> bool:
    """Restituisce True se l'email sembra un mittente automatico da ignorare."""
    email = email.lower()
    return any(re.search(p, email) for p in SKIP_PATTERNS)


def extract_company_from_domain(domain: str) -> str:
    """Ricava un nome azienda plausibile dal dominio email."""
    # Rimuove TLD comuni e capitalizza
    name = domain.split(".")[0]
    # Rimuove prefissi generici
    for prefix in ("mail", "smtp", "send", "email", "info"):
        if name == prefix and len(domain.split(".")) > 2:
            name = domain.split(".")[1]
    return name.replace("-", " ").replace("_", " ").title()


def build_hubspot_properties(sender: EmailSender, existing: Optional[dict]) -> dict:
    """
    Costruisce le proprietà HubSpot da aggiornare/creare per un contatto.
    Per i contatti esistenti aggiorna solo i campi vuoti.
    """
    props = {}

    if existing:
        # Aggiorna solo i campi mancanti
        if not existing.get("firstname") and sender.firstname:
            props["firstname"] = sender.firstname
        if not existing.get("lastname") and sender.lastname:
            props["lastname"] = sender.lastname
        if not existing.get("company") and sender.company_guess:
            props["company"] = sender.company_guess
    else:
        # Crea con tutti i campi disponibili
        if sender.firstname:
            props["firstname"] = sender.firstname
        if sender.lastname:
            props["lastname"] = sender.lastname
        props["email"] = sender.email
        if sender.company_guess:
            props["company"] = sender.company_guess

    return props


def build_activity_note(sender: EmailSender, email_count: int) -> str:
    """Genera il testo della nota attività da associare al contatto."""
    lines = [
        f"📧 {NOTE_TAG} — Email ricevuta da {sender.email}",
        f"",
        f"Tag: {NOTE_TAG}",
        f"Fonte contatto: Gmail",
        f"Data ultima email: {sender.latest_date[:10]}",
        f"Email ricevute (ultime 24h): {email_count}",
    ]
    if sender.company_guess:
        lines.append(f"Azienda (da dominio): {sender.company_guess}")
    lines.append("")
    lines.append("Sincronizzato automaticamente da routine Gmail→HubSpot.")
    return "\n".join(lines)


# ─── Flusso principale (pseudocodice eseguito via MCP in sessione Claude) ──────

def run_sync():
    """
    Flusso di sincronizzazione (eseguito manualmente tramite sessione Claude Code
    con accesso ai tool MCP Gmail e HubSpot).

    Passi:
      1. Cerca email in inbox con query Gmail (newer_than:1d, -from:me)
      2. Raggruppa per mittente, ignora automatici
      3. Per ogni mittente unico:
         a. Cerca contatto HubSpot per email (chiave univoca)
         b. Se esiste → aggiorna campi mancanti + aggiunge nota attività
         c. Se non esiste → crea nuovo contatto + aggiunge nota attività
      4. Logga risultati: Creato / Aggiornato / Ignorato
    """
    results: list[SyncResult] = []

    # [Eseguito via tool MCP Gmail / HubSpot nella sessione Claude Code]
    # Vedere SYNC_LOG.md per i risultati dell'ultima esecuzione.

    return results


# ─── Report ────────────────────────────────────────────────────────────────────

def format_report(results: list[SyncResult]) -> str:
    lines = ["# Risultati Sync Gmail → HubSpot", ""]
    lines.append(f"| Stato | Email | ID HubSpot | Note |")
    lines.append(f"|-------|-------|------------|------|")
    for r in results:
        lines.append(
            f"| {r.status} | {r.email} | {r.hubspot_id or '-'} | {r.reason or ''} |"
        )
    return "\n".join(lines)
