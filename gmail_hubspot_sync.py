#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts sender contacts, and syncs them to HubSpot.

Requires:
  pip install anthropic

Run:
  python gmail_hubspot_sync.py

The script uses Claude claude-sonnet-4-6 as the agent with MCP tools for Gmail and HubSpot.
Configure ANTHROPIC_API_KEY and MCP server URLs in the environment before running.
"""

import os
import re
import json
from anthropic import Anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SYNC_PROMPT = """
Esegui la sincronizzazione Gmail → HubSpot seguendo questi passi NELL'ORDINE:

1. **Recupera email in arrivo**
   - Usa `mcp__Gmail__search_threads` con query: `in:inbox -from:me newer_than:30d`
   - Recupera fino a 50 thread

2. **Estrai mittenti unici**
   Per ogni thread/messaggio nell'inbox:
   - Prendi il campo `sender` di ogni messaggio con labelId INBOX
   - Per le email con sender `redazione@latestata.it`, leggi lo snippet per estrarre
     il mittente originale (formato: Da "Nome" email@dominio.it)
   - Escludi mittenti di sistema:
     * mailer-daemon@googlemail.com
     * *@googlemail.com (notifiche)
     * *@facebookmail.com
     * *-noreply@*.com
     * pubblica.latestata@gmail.com (account proprietario)
     * cristian.mameli.editore@gmail.com (account proprietario)
     * redazione@latestata.it (account interno)

3. **Per ogni mittente unico**, estrai:
   - `email`: indirizzo email (obbligatorio)
   - `firstname`: nome (se ricavabile dalla firma o dall'indirizzo)
   - `lastname`: cognome (se ricavabile)
   - `company`: azienda (usa dominio email se non è gmail/libero/alice/hotmail/yahoo)

4. **Verifica in HubSpot** se il contatto esiste:
   - Usa `mcp__HubSpot__search_crm_objects` con filterGroups[].filters[].operator = "IN"
     passando l'array di tutte le email da verificare
   - Nota gli ID dei contatti già presenti

5. **Crea contatti nuovi** (email non trovate in HubSpot):
   - Usa `mcp__HubSpot__manage_crm_objects` → createRequest
   - Campi da compilare:
     * email
     * firstname (se disponibile)
     * lastname (se disponibile)
     * company (se disponibile)
     * hs_analytics_source: "EMAIL_MARKETING"
   - confirmationStatus: "CONFIRMATION_WAIVED_FOR_SESSION"

6. **Aggiorna contatti esistenti** che mancano di `hs_analytics_source`:
   - Usa `mcp__HubSpot__manage_crm_objects` → updateRequest
   - Imposta `hs_analytics_source: "EMAIL_MARKETING"` dove non è già impostato
   - confirmationStatus: "CONFIRMATION_WAIVED_FOR_SESSION"

7. **Output finale** in formato tabella Markdown:

| Stato | Email Contatto | ID HubSpot | Nome | Azienda |
|-------|---------------|------------|------|---------|
| Creato | email@... | 123456 | Nome Cognome | Azienda |
| Aggiornato | email@... | 789012 | Nome | - |
| Ignorato | email@... | 345678 | Nome | Azienda |

Esegui tutti i passi autonomamente senza chiedere conferma intermedie.
"""


def run_sync(dry_run: bool = False) -> str:
    """Run the Gmail → HubSpot sync using Claude as the agent."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    mcp_servers = [
        {
            "type": "url",
            "url": os.environ.get("GMAIL_MCP_URL", ""),
            "name": "Gmail",
        },
        {
            "type": "url",
            "url": os.environ.get("HUBSPOT_MCP_URL", ""),
            "name": "HubSpot",
        },
    ]

    prompt = SYNC_PROMPT
    if dry_run:
        prompt += "\n\nMODALITÀ DRY RUN: mostra i contatti che verrebbero creati/aggiornati senza effettuare modifiche."

    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        mcp_servers=mcp_servers,
        messages=[{"role": "user", "content": prompt}],
        betas=["mcp-client-2025-04-04"],
    )

    result_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            result_parts.append(block.text)

    return "\n".join(result_parts)


def parse_sender_from_snippet(snippet: str) -> tuple[str, str]:
    """
    Extracts email and name from Gmail snippet of a forwarded message.
    Example snippet: 'Da "Nome Cognome" email@domain.com A ...'
    Returns (email, display_name).
    """
    pattern = r'Da\s+"([^"]+)"\s+([\w.+\-]+@[\w.\-]+)'
    match = re.search(pattern, snippet)
    if match:
        return match.group(2).lower(), match.group(1).strip()

    email_pattern = r'Da\s+([\w.+\-]+@[\w.\-]+)'
    match = re.search(email_pattern, snippet)
    if match:
        return match.group(1).lower(), ""

    return "", ""


def extract_company_from_domain(email: str) -> str:
    """
    Infers company name from the email domain.
    Returns empty string for generic providers (gmail, libero, hotmail, yahoo, etc.)
    """
    generic_domains = {
        "gmail.com", "libero.it", "hotmail.com", "hotmail.it", "yahoo.com",
        "yahoo.it", "alice.it", "virgilio.it", "tiscali.it", "live.com",
        "outlook.com", "icloud.com", "me.com",
    }
    domain = email.split("@")[-1].lower()
    if domain in generic_domains:
        return ""

    parts = domain.split(".")
    company = parts[-2] if len(parts) >= 2 else domain
    return company.replace("-", " ").replace("_", " ").title()


def split_name(display_name: str) -> tuple[str, str]:
    """Splits a display name into firstname and lastname."""
    if not display_name:
        return "", ""
    parts = display_name.strip().split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


SYSTEM_SENDERS = {
    "mailer-daemon@googlemail.com",
    "pubblica.latestata@gmail.com",
    "cristian.mameli.editore@gmail.com",
    "redazione@latestata.it",
}

SYSTEM_SENDER_PATTERNS = [
    re.compile(r".*-noreply@.*"),
    re.compile(r"noreply@.*"),
    re.compile(r"no-reply@.*"),
    re.compile(r"notification@.*"),
    re.compile(r"analytics-.*@.*"),
    re.compile(r".*@facebookmail\.com"),
    re.compile(r".*@googlemail\.com"),
]


def is_system_sender(email: str) -> bool:
    email = email.lower().strip()
    if email in SYSTEM_SENDERS:
        return True
    return any(p.match(email) for p in SYSTEM_SENDER_PATTERNS)


if __name__ == "__main__":
    import sys

    dry = "--dry-run" in sys.argv
    if not ANTHROPIC_API_KEY:
        print("Errore: ANTHROPIC_API_KEY non configurata.")
        sys.exit(1)

    print("Avvio sincronizzazione Gmail → HubSpot...")
    result = run_sync(dry_run=dry)
    print(result)
