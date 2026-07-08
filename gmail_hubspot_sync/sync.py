"""
Gmail → HubSpot Contact Sync
=============================
Monitora le email in arrivo su Gmail, estrae i mittenti e li sincronizza
automaticamente in HubSpot evitando duplicati.

Logica principale:
  1. Cerca email non etichettate con "HubSpot-Synced" (non ancora processate)
  2. Per ogni email estrae: email, nome, dominio/azienda del mittente
  3. Cerca il contatto in HubSpot tramite email (chiave unica)
  4. Se esiste → aggiorna i campi mancanti
  5. Se non esiste → crea nuovo contatto
  6. Applica label Gmail "HubSpot-Synced" per evitare ri-elaborazione

Esecuzione:
  Questo script è progettato per essere eseguito da Claude Code come prompt
  CronCreate ogni 10 minuti. Non è uno script Python standalone — la logica
  viene orchestrata attraverso i tool MCP Gmail e HubSpot.

Campi HubSpot compilati:
  - email           (obbligatorio)
  - firstname       (dal nome mittente, se disponibile)
  - lastname        (dal cognome mittente, se disponibile)
  - company         (dal dominio email, es. "example.com" → "Example")
  - hs_lead_status  (impostato a "NEW")
  - leadsource      (impostato a "Gmail")
  - hs_tag_ids / note: tag "Inbound Gmail"

Stato output per ogni email:
  - CREATO    : nuovo contatto aggiunto in HubSpot
  - AGGIORNATO: contatto esistente aggiornato con dati mancanti
  - IGNORATO  : mittente già completo o email di sistema da skippare
"""

SKIP_DOMAINS = {
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "bounce", "notifications", "alerts", "automated",
}

GMAIL_LABEL = "HubSpot-Synced"


def extract_name_parts(display_name: str) -> tuple[str, str]:
    """Divide 'Mario Rossi' → ('Mario', 'Rossi')."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def company_from_domain(domain: str) -> str:
    """Ricava un nome azienda dal dominio, es. 'acme.com' → 'Acme'."""
    base = domain.split(".")[0]
    return base.capitalize()


def should_skip(local_part: str, domain: str) -> bool:
    """Ritorna True se il mittente è un indirizzo di sistema da ignorare."""
    return local_part.lower() in SKIP_DOMAINS or domain.split(".")[0].lower() in SKIP_DOMAINS
