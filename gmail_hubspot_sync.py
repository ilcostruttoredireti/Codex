"""
Gmail → HubSpot Contact Sync Routine
Monitora le email in arrivo su Gmail ed estrae/sincronizza i contatti in HubSpot.
"""

import re
from dataclasses import dataclass
from typing import Optional

IGNORED_DOMAINS = {
    "facebookmail.com", "googlemail.com", "notifications.google.com",
    "bounce.com", "noreply.com", "mailer-daemon.googlemail.com",
}
IGNORED_PREFIXES = {"noreply", "no-reply", "notification", "mailer-daemon", "bounce"}
PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it", "virgilio.it", "tiscali.it"}

OWNER_EMAIL = "cristian.mameli.editore@gmail.com"

GMAIL_QUERY = "in:inbox newer_than:1d -from:me"
GMAIL_SOURCE_LABEL = "Gmail"
HUBSPOT_TAG = "Inbound Gmail"


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str
    is_business: bool


def parse_sender(from_header: str) -> Optional[SenderContact]:
    """Estrae dati strutturati dall'header From di un'email."""
    email_match = re.search(r'[\w.+-]+@[\w.-]+\.\w+', from_header)
    if not email_match:
        return None

    email = email_match.group(0).lower()
    domain = email.split("@")[1]

    # Ignora mittenti automatici/notifiche
    local_part = email.split("@")[0]
    if any(local_part.startswith(p) for p in IGNORED_PREFIXES):
        return None
    if domain in IGNORED_DOMAINS:
        return None

    # Estrai nome dal pattern "Nome Cognome <email>"
    name_match = re.match(r'"?([^<"]+)"?\s*<', from_header)
    display_name = name_match.group(1).strip() if name_match else ""

    parts = display_name.split() if display_name else []
    first_name = parts[0] if parts else None
    last_name = " ".join(parts[1:]) if len(parts) > 1 else None

    is_business = domain not in PERSONAL_DOMAINS
    company = None
    if is_business:
        company = domain.split(".")[0].replace("-", " ").title()

    return SenderContact(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        is_business=is_business,
    )


def build_hubspot_properties(sender: SenderContact) -> dict:
    """Costruisce i properties HubSpot da un SenderContact."""
    props = {
        "email": sender.email,
        "lead_source": GMAIL_SOURCE_LABEL,
    }
    if sender.first_name:
        props["firstname"] = sender.first_name
    if sender.last_name:
        props["lastname"] = sender.last_name
    if sender.company:
        props["company"] = sender.company
    return props


# -------------------------------------------------------------------
# Logica di esecuzione (implementata tramite strumenti MCP in Claude)
# -------------------------------------------------------------------
# Il flusso reale viene eseguito dall'agente Claude usando i tool MCP:
#
# 1. mcp__Gmail__search_threads(query=GMAIL_QUERY, pageSize=50)
# 2. Per ogni thread → mcp__Gmail__get_thread(messageFormat="METADATA_ONLY")
#    → estrae sender, chiama parse_sender()
# 3. mcp__HubSpot__search_crm_objects filtrando per email IN [lista]
# 4. Se contatto NON esiste → mcp__HubSpot__manage_crm_objects (createRequest)
# 5. Se contatto esiste con campi mancanti → mcp__HubSpot__manage_crm_objects (updateRequest)
# 6. Se contatto esiste e completo → IGNORATO
# -------------------------------------------------------------------
