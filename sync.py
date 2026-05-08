"""Core Gmail → HubSpot sync logic."""
import logging
from dataclasses import dataclass

from gmail_client import GmailClient, SenderInfo
from hubspot_client import ContactResult, HubSpotClient

logger = logging.getLogger(__name__)

# Well-known personal/freemail domains where the domain name is not a useful
# company name.
_FREEMAIL_DOMAINS = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.it",
        "yahoo.co.uk",
        "hotmail.com",
        "hotmail.it",
        "outlook.com",
        "live.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "libero.it",
        "tin.it",
        "virgilio.it",
        "alice.it",
        "tiscali.it",
    ]
)


def _company_from_domain(domain: str) -> str:
    """Convert 'acme.com' → 'acme' as a fallback company name."""
    if not domain or domain in _FREEMAIL_DOMAINS:
        return ""
    # Strip TLD(s): acme.co.uk → acme
    parts = domain.split(".")
    return parts[0].capitalize() if parts else ""


class Syncer:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        ignored_domains: list[str] | None = None,
    ) -> None:
        self._gmail = gmail
        self._hubspot = hubspot
        self._ignored_domains = set(ignored_domains or [])

    def run_once(self) -> list[ContactResult]:
        """Process all unprocessed inbox emails and return per-message results."""
        results: list[ContactResult] = []

        for sender in self._gmail.iter_unprocessed_senders():
            result = self._sync_sender(sender)
            results.append(result)
            _log_result(result)

        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sync_sender(self, sender: SenderInfo) -> ContactResult:
        email = sender.email

        if sender.domain in self._ignored_domains:
            logger.info("Ignored (domain filter): %s", email)
            return ContactResult(status="ignored", contact_email=email, contact_id=None)

        company = _company_from_domain(sender.domain)

        existing = self._hubspot.find_contact_by_email(email)

        if existing is None:
            contact_id = self._hubspot.create_contact(
                email=email,
                first_name=sender.first_name,
                last_name=sender.last_name,
                company=company,
            )
            if contact_id:
                self._hubspot.log_email_received(contact_id, email, sender.subject)
                return ContactResult(
                    status="created", contact_email=email, contact_id=contact_id
                )
            return ContactResult(status="ignored", contact_email=email, contact_id=None)

        contact_id = existing.id
        existing_props = existing.properties or {}

        updated = self._hubspot.update_contact(
            contact_id=contact_id,
            email=email,
            first_name=sender.first_name,
            last_name=sender.last_name,
            company=company,
            existing_props=existing_props,
        )
        self._hubspot.log_email_received(contact_id, email, sender.subject)

        status = "updated" if updated else "ignored"
        return ContactResult(status=status, contact_email=email, contact_id=contact_id)


def _log_result(result: ContactResult) -> None:
    icons = {"created": "+", "updated": "~", "ignored": "="}
    icon = icons.get(result.status, "?")
    logger.info(
        "[%s] %-8s | email: %-40s | id: %s",
        icon,
        result.status.upper(),
        result.contact_email,
        result.contact_id or "—",
    )
