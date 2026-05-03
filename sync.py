import os
import logging
from dataclasses import dataclass
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

# Domains that belong to free email providers or automated senders — skip these.
_DEFAULT_IGNORED = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "icloud.com", "me.com", "aol.com",
    "protonmail.com", "proton.me", "libero.it", "alice.it", "virgilio.it",
    "tiscali.it",
}


def _ignored_domains() -> set[str]:
    extra = os.getenv("IGNORED_DOMAINS_EXTRA", "")
    extras = {d.strip() for d in extra.split(",") if d.strip()}
    return _DEFAULT_IGNORED | extras


def _is_automated_sender(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(kw in local for kw in ("noreply", "no-reply", "mailer-daemon", "bounce", "postmaster", "donotreply"))


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: str | None
    reason: str = ""


class EmailContactSync:
    def __init__(self):
        self.gmail = GmailClient()
        self.hubspot = HubSpotClient()
        self._ignored_domains = _ignored_domains()

    def process_message(self, message_id: str) -> SyncResult:
        details = self.gmail.get_message_details(message_id)
        sender_email = details["from_email"]
        sender_name = details["from_name"]

        if self._should_skip(sender_email):
            return SyncResult("Ignorato", sender_email, None, "mittente automatico o dominio generico")

        firstname, lastname = self._split_name(sender_name)
        company = self._company_from_domain(sender_email)

        contact = self.hubspot.find_contact_by_email(sender_email)

        if contact:
            return self._update_existing(contact, firstname, lastname, company)
        else:
            return self._create_new(sender_email, firstname, lastname, company)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _should_skip(self, email: str) -> bool:
        domain = email.split("@")[-1].lower() if "@" in email else ""
        return domain in self._ignored_domains or _is_automated_sender(email)

    def _update_existing(self, contact, firstname: str, lastname: str, company: str) -> SyncResult:
        props = contact.properties
        patch = {}
        if not props.get("firstname") and firstname:
            patch["firstname"] = firstname
        if not props.get("lastname") and lastname:
            patch["lastname"] = lastname
        if not props.get("company") and company:
            patch["company"] = company

        if patch:
            self.hubspot.update_contact(contact.id, patch)
            return SyncResult("Aggiornato", props["email"], contact.id)

        return SyncResult("Ignorato", props["email"], contact.id, "nessun campo da aggiornare")

    def _create_new(self, email: str, firstname: str, lastname: str, company: str) -> SyncResult:
        result = self.hubspot.create_contact({
            "email": email,
            "firstname": firstname,
            "lastname": lastname,
            "company": company,
        })
        if result:
            return SyncResult("Creato", email, result.id)
        return SyncResult("Ignorato", email, None, "errore creazione HubSpot")

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str]:
        parts = full_name.strip().split(None, 1) if full_name else []
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")

    @staticmethod
    def _company_from_domain(email: str) -> str:
        domain = email.split("@")[-1].lower() if "@" in email else ""
        root = domain.split(".")[0] if domain else ""
        return root.capitalize() if root else ""
