from config import PERSONAL_EMAIL_DOMAINS


def get_domain(email: str) -> str:
    if "@" in email:
        return email.split("@", 1)[1].lower()
    return ""


def split_full_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from an email domain.

    Strips the TLD and common mail sub-domains, then capitalises the
    remaining leftmost label (e.g. ``mail.acme.com`` → ``Acme``).
    """
    parts = domain.split(".")
    # Drop leading service labels
    while parts and parts[0] in ("www", "mail", "smtp", "info", "support", "help"):
        parts = parts[1:]
    if len(parts) >= 2:
        # e.g. ["acme", "com"] → "Acme"
        return parts[0].capitalize()
    if parts:
        return parts[0].capitalize()
    return ""


def parse_contact(name: str, email: str) -> dict:
    """Return a normalised contact dict extracted from a sender header.

    Keys: ``email``, ``first_name``, ``last_name``, ``company``, ``domain``.
    """
    domain = get_domain(email)
    first_name, last_name = split_full_name(name)

    company = ""
    if domain and domain not in PERSONAL_EMAIL_DOMAINS:
        company = company_from_domain(domain)

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }
