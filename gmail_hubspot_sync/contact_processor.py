import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from config import IGNORED_DOMAINS, IGNORED_EMAIL_PREFIXES
from hubspot_client import ContactData, HubSpotClient
from utils import company_from_domain, extract_email_parts, is_valid_email, parse_name

logger = logging.getLogger("gmail_hubspot_sync.processor")


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class ProcessResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str] = None
    reason: Optional[str] = None


class ContactProcessor:
    def __init__(self, hubspot: HubSpotClient) -> None:
        self._hs = hubspot

    def process(self, raw_from: str) -> ProcessResult:
        """
        Parse a raw From header, decide create/update/ignore, act, return result.
        """
        email, display_name = extract_email_parts(raw_from)

        if not is_valid_email(email):
            return ProcessResult(SyncStatus.IGNORED, email, reason="Invalid email format")

        domain = email.split("@")[-1].lower()
        prefix = email.split("@")[0].lower()

        if domain in IGNORED_DOMAINS:
            return ProcessResult(SyncStatus.IGNORED, email, reason=f"Ignored domain: {domain}")

        if any(prefix.startswith(p) for p in IGNORED_EMAIL_PREFIXES):
            return ProcessResult(SyncStatus.IGNORED, email, reason=f"Ignored prefix: {prefix}")

        first_name, last_name = parse_name(display_name)
        company = company_from_domain(domain)

        data = ContactData(
            email=email,
            first_name=first_name,
            last_name=last_name,
            company=company,
        )

        existing = self._hs.find_contact_by_email(email)

        if existing is None:
            contact_id = self._hs.create_contact(data)
            if contact_id:
                return ProcessResult(SyncStatus.CREATED, email, contact_id=contact_id)
            return ProcessResult(SyncStatus.IGNORED, email, reason="HubSpot create failed")

        contact_id = existing.get("id") or existing.get("properties", {}).get("hs_object_id")
        updated = self._hs.update_contact(str(contact_id), existing, data)
        status = SyncStatus.UPDATED if updated else SyncStatus.IGNORED
        reason = None if updated else "No new fields to update"
        return ProcessResult(status, email, contact_id=str(contact_id), reason=reason)
