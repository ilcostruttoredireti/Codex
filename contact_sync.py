"""Core sync logic: email sender → HubSpot contact."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

# Providers whose domain should NOT be used as company name
_PERSONAL_DOMAINS: set[str] = set()


@dataclass
class SyncResult:
    status: Literal["created", "updated", "ignored"]
    email: str
    hubspot_id: str | None
    reason: str = ""

    def __str__(self) -> str:
        icon = {"created": "✚", "updated": "↻", "ignored": "–"}[self.status]
        base = f"[{icon}] {self.status.upper():8s} | {self.email:40s} | id={self.hubspot_id or 'n/a'}"
        return base + (f"  ({self.reason})" if self.reason else "")


class ContactSync:
    def __init__(
        self,
        hubspot: HubSpotClient,
        ignored_domains: set[str] | None = None,
        add_timeline_note: bool = True,
    ):
        self._hs = hubspot
        self._ignored_domains = ignored_domains or set()
        self._add_timeline_note = add_timeline_note

    # ── Public API ────────────────────────────────────────────────────────────

    def process(self, email: str, name: str, subject: str = "") -> SyncResult:
        """
        Decide whether to create/update/ignore a contact and act accordingly.
        Returns a SyncResult describing what happened.
        """
        email = email.strip().lower()
        domain = _extract_domain(email)

        if domain in self._ignored_domains:
            return SyncResult("ignored", email, None, reason="personal domain")

        firstname, lastname = _split_name(name)
        company = _company_from_domain(domain, self._ignored_domains)

        existing = self._hs.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            updates = self._build_updates(existing["properties"], firstname, lastname, company)
            if updates:
                self._hs.update_contact(contact_id, updates)
                status: Literal["created", "updated", "ignored"] = "updated"
            else:
                status = "ignored"

            if self._add_timeline_note and subject:
                self._add_note(contact_id, email, subject)

            return SyncResult(status, email, contact_id,
                              reason="fields updated" if updates else "no changes")

        # Create new contact
        properties = {
            "email": email,
            "lead_source": "Gmail",
        }
        if firstname:
            properties["firstname"] = firstname
        if lastname:
            properties["lastname"] = lastname
        if company:
            properties["company"] = company

        created = self._hs.create_contact(properties)
        contact_id = created["id"]

        if self._add_timeline_note:
            self._add_note(contact_id, email, subject)

        return SyncResult("created", email, contact_id)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_updates(
        self,
        existing: dict[str, str],
        firstname: str,
        lastname: str,
        company: str,
    ) -> dict[str, str]:
        """Return only the properties that are currently blank and have a new value."""
        updates: dict[str, str] = {}

        def _fill(key: str, value: str) -> None:
            if value and not existing.get(key):
                updates[key] = value

        _fill("firstname", firstname)
        _fill("lastname", lastname)
        _fill("company", company)

        # Always stamp lead_source if missing
        if not existing.get("lead_source"):
            updates["lead_source"] = "Gmail"

        return updates

    def _add_note(self, contact_id: str, email: str, subject: str) -> None:
        body = f"Email received from {email}"
        if subject:
            body += f'\nSubject: "{subject}"'
        body += "\n\nSource: Inbound Gmail"
        try:
            self._hs.create_note(contact_id, body)
        except Exception as exc:
            logger.warning("Could not create note for %s: %s", contact_id, exc)


# ── Utility functions ─────────────────────────────────────────────────────────


def _extract_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower() if "@" in email else ""


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'John Doe' into ('John', 'Doe'). Returns ('', '') if blank."""
    full_name = full_name.strip()
    if not full_name:
        return "", ""
    parts = full_name.split(None, 1)  # split on first whitespace
    firstname = parts[0]
    lastname = parts[1] if len(parts) > 1 else ""
    return firstname, lastname


def _company_from_domain(domain: str, ignored_domains: set[str]) -> str:
    """
    Derive a company name from a business email domain.
    e.g. 'acme-corp.com' → 'Acme Corp'
    Returns '' for personal/generic domains.
    """
    if not domain or domain in ignored_domains:
        return ""
    # Strip TLD suffix and capitalise
    base = domain.rsplit(".", 1)[0]
    # Replace hyphens/underscores with spaces and title-case
    company = re.sub(r"[-_]+", " ", base).title()
    return company
