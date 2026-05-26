"""
Funzioni di parsing pure — senza dipendenze esterne.
Testabili in isolamento (nessun import Google / HubSpot).
"""
import re


def parse_from_header(from_header: str) -> tuple[str, str]:
    """
    Estrae (nome_display, indirizzo_email) dall'header From.

    Es: '"Mario Rossi" <mario@example.com>' → ('Mario Rossi', 'mario@example.com')
    Es: 'mario@example.com'                 → ('', 'mario@example.com')
    """
    from_header = from_header.strip()

    # Formato: "Nome" <email>  o  Nome <email>
    match = re.match(r'^(.*?)\s*<([^>]+)>\s*$', from_header)
    if match:
        display = match.group(1).strip().strip('"').strip("'")
        addr = match.group(2).strip().lower()
        return display, addr

    # Solo indirizzo email
    if "@" in from_header:
        return "", from_header.strip().lower()

    return "", ""


def parse_name(display_name: str) -> tuple[str, str]:
    """
    Estrae (nome, cognome) da una stringa 'Nome Cognome'.

    Es: 'Mario Rossi'         → ('Mario', 'Rossi')
    Es: 'Anna Maria De Luca'  → ('Anna', 'Maria De Luca')
    """
    display_name = display_name.strip().strip('"').strip("'")
    parts = display_name.split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_domain(email_addr: str) -> str:
    """Restituisce il dominio dall'indirizzo email in minuscolo."""
    if "@" in email_addr:
        return email_addr.split("@", 1)[1].lower().strip()
    return ""


def domain_to_company(domain: str) -> str:
    """
    Ricava un nome aziendale approssimativo dal dominio.

    Es: 'acme-corp.com' → 'Acme Corp'
    Es: 'startup.io'   → 'Startup'
    """
    if not domain:
        return ""
    # rimuove TLD (ultima parte dopo l'ultimo punto)
    name_part = domain.rsplit(".", 1)[0]
    # sostituisce trattini/underscore con spazi, capitalizza ogni parola
    return " ".join(w.capitalize() for w in re.split(r"[-_]", name_part))
