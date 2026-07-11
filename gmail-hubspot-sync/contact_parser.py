"""
Parse email sender info into structured contact data.
Detects and skips automated/no-reply senders.
"""
from email.utils import parseaddr

AUTOMATED_PREFIXES = (
    "no-reply", "noreply", "nobody", "pinbot", "mailer", "ads-",
    "notifications", "notification", "donotreply", "do-not-reply",
    "unsubscribe", "bounce", "daemon", "postmaster", "automailer",
    "auto-", "newsletter", "info-", "updates", "alerts",
)

AUTOMATED_DOMAINS = frozenset([
    "youtube.com", "google.com", "accounts.google.com", "discord.com",
    "skool.com", "coinlist.co", "serpapi.com", "bybit.com",
    "academia-mail.com", "notification.circle.so", "engage.canva.com",
    "account.canva.com", "info.pinterest.com", "e.feedspot.com",
    "email.openai.com", "ebay.com", "github.com", "stripe.com",
    "mailchimp.com", "hubspot.com", "moneya.es", "sendgrid.net",
    "amazonses.com", "twilio.com", "salesforce.com",
])

# Map common email prefixes to human-readable first names
PREFIX_FIRST_NAMES = {
    "formazione": "Formazione",
    "commerciale": "Commerciale",
    "redazione": "Redazione",
    "staff": "Staff",
    "support": "Support",
    "info": "Info",
    "contact": "Contact",
    "general": "General",
    "hello": "Hello",
    "ciao": "Ciao",
    "team": "Team",
    "daily": "Daily",
}


class ContactParser:
    def parse(self, message: dict) -> dict | None:
        """
        Parse a raw Gmail message dict into a contact dict.
        Returns None if no valid email can be extracted.
        """
        sender = message.get("sender", "")
        raw_name, email = parseaddr(sender)

        # Fallback: treat the whole sender string as an email
        if not email and "@" in sender:
            email = sender.strip()
            raw_name = ""

        if not email or "@" not in email:
            return None

        email = email.lower().strip()
        domain = email.split("@")[1]
        local = email.split("@")[0]

        company = self._domain_to_company(domain)
        first, last = self._parse_name(raw_name, local, domain)

        return {
            "email": email,
            "first_name": first,
            "last_name": last,
            "company": company,
            "domain": domain,
            "raw_name": raw_name,
        }

    def is_automated(self, contact: dict) -> bool:
        """Return True if the contact looks like an automated/no-reply sender."""
        if not contact or not contact.get("email"):
            return True

        local = contact["email"].split("@")[0].lower()
        domain = contact.get("domain", "").lower()

        for prefix in AUTOMATED_PREFIXES:
            if local.startswith(prefix):
                return True

        for auto_domain in AUTOMATED_DOMAINS:
            if domain == auto_domain or domain.endswith("." + auto_domain):
                return True

        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _domain_to_company(self, domain: str) -> str:
        parts = domain.split(".")
        # Strip common subdomains
        if parts[0] in ("mail", "email", "info", "news", "blog", "www", "smtp", "c", "m"):
            parts = parts[1:]
        core = parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")
        name = core.replace("-", " ").replace("_", " ")
        return " ".join(w.capitalize() for w in name.split())

    def _parse_name(self, raw_name: str, local: str, domain: str) -> tuple[str, str]:
        if raw_name:
            parts = raw_name.strip().split()
            if len(parts) >= 2:
                return parts[0], " ".join(parts[1:])
            if parts:
                return parts[0], ""

        # Infer from email local part (e.g. "riccardo" -> first="Riccardo")
        if "." in local:
            segments = local.split(".")
            return segments[0].capitalize(), segments[1].capitalize()

        if local in PREFIX_FIRST_NAMES:
            return PREFIX_FIRST_NAMES[local], ""

        return local.capitalize(), ""
