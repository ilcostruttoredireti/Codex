"""Core Gmail → HubSpot contact sync logic."""

import logging
import re
from dataclasses import dataclass
from enum import Enum

from gmail_client import GmailClient
from hubspot_client import ContactData, HubSpotClient

logger = logging.getLogger(__name__)

# Domains whose contacts we never import (noise reduction).
IGNORED_DOMAINS = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.it",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "icloud.com",
        "me.com",
        "noreply.com",
        "no-reply.com",
        "mailer-daemon.com",
        "notifications.google.com",
    ]
)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str | None
    reason: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status.value}] {self.email}"]
        if self.contact_id:
            parts.append(f"id={self.contact_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " | ".join(parts)


def _domain_from_email(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else ""


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Handles single-word names."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from the email domain."""
    # Strip common TLDs and format.
    name = re.sub(r"\.(com|net|org|io|co|it|eu|de|fr|es|uk|biz|info)(\.\w+)?$", "", domain)
    return name.replace("-", " ").replace("_", " ").title()


class ContactSyncer:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        *,
        ignore_free_email: bool = True,
        log_timeline: bool = False,
        timeline_app_id: int | None = None,
        timeline_template_id: str | None = None,
    ):
        self.gmail = gmail
        self.hubspot = hubspot
        self.ignore_free_email = ignore_free_email
        self.log_timeline = log_timeline
        self.timeline_app_id = timeline_app_id
        self.timeline_template_id = timeline_template_id

    def run_once(self) -> list[SyncResult]:
        """Poll Gmail once and sync all new senders to HubSpot."""
        results: list[SyncResult] = []

        for sender in self.gmail.poll_new_messages():
            result = self._process_sender(sender)
            results.append(result)
            logger.info(str(result))

        return results

    def _process_sender(self, sender: dict) -> SyncResult:
        email = sender["email"]
        name = sender.get("name", "")
        subject = sender.get("subject", "")
        domain = _domain_from_email(email)

        if self.ignore_free_email and domain in IGNORED_DOMAINS:
            return SyncResult(SyncStatus.IGNORED, email, None, "free/noreply domain")

        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            return SyncResult(SyncStatus.IGNORED, email, None, "invalid email format")

        first_name, last_name = _split_name(name)
        company = _company_from_domain(domain) if domain not in IGNORED_DOMAINS else ""

        contact_data = ContactData(
            email=email,
            first_name=first_name,
            last_name=last_name,
            company=company,
        )

        existing = self.hubspot.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            self.hubspot.update_contact(contact_id, existing.get("properties", {}), contact_data)
            status = SyncStatus.UPDATED
        else:
            contact_id = self.hubspot.create_contact(contact_data)
            status = SyncStatus.CREATED

        if self.log_timeline and self.timeline_app_id and self.timeline_template_id:
            self.hubspot.log_email_activity(
                contact_id, self.timeline_app_id, self.timeline_template_id, subject
            )

        return SyncResult(status, email, contact_id)
