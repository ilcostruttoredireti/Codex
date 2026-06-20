"""Gmail → HubSpot contact sync routine.

For every inbound email:
  1. Extracts sender (email, name, company domain)
  2. Searches HubSpot by email
  3. Creates contact if not found, updates missing fields if found
  4. Tags contact with lead_source=Gmail and tag 'Inbound Gmail'
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Sender:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""

    @property
    def company_from_domain(self) -> str:
        """Derives a readable company name from the email domain."""
        if not self.domain or self.domain in (
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it",
            "virgilio.it", "tiscali.it",
        ):
            return ""
        # strip TLD and convert hyphens/dots to spaces
        base = re.sub(r"\.[a-z]{2,4}$", "", self.domain)
        base = re.sub(r"[.\-_]+", " ", base)
        return base.title()


@dataclass
class SyncResult:
    status: Literal["Creato", "Aggiornato", "Ignorato", "Saltato"]
    email: str
    hubspot_id: str | None = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Addresses to never import (automated senders, own mailbox, etc.)
SKIP_DOMAINS = {
    "facebookmail.com", "notifications.google.com", "bounce.em.hubspot.com",
    "mailer-daemon",
}
SKIP_PREFIXES = ("noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster")


def should_skip(email: str, own_emails: set[str]) -> bool:
    email_lower = email.lower()
    if email_lower in own_emails:
        return True
    local, _, domain = email_lower.partition("@")
    if domain in SKIP_DOMAINS:
        return True
    if any(local.startswith(p) for p in SKIP_PREFIXES):
        return True
    return False


def parse_display_name(display_name: str) -> tuple[str, str]:
    """Returns (firstname, lastname) from a display name string."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ---------------------------------------------------------------------------
# Core sync logic (framework-agnostic — tools injected via parameters)
# ---------------------------------------------------------------------------

def build_hubspot_properties(sender: Sender, existing: dict | None) -> dict:
    """Returns only the properties that are missing or empty on the existing record."""
    company = sender.company or sender.company_from_domain
    candidates = {
        "email": sender.email,
        "firstname": sender.firstname,
        "lastname": sender.lastname,
        "company": company,
        "lead_source": "Gmail",
    }
    if existing is None:
        return candidates

    # Only patch fields that are not already set
    props = existing.get("properties", {})
    update = {}
    for key, value in candidates.items():
        if value and not props.get(key):
            update[key] = value
    return update


def sync_contacts(
    emails: list[tuple[str, str]],   # [(email, display_name), ...]
    own_emails: set[str],
    search_fn,                        # (email) -> dict | None
    create_fn,                        # (props) -> str  (returns HS id)
    update_fn,                        # (hs_id, props) -> None
) -> list[SyncResult]:
    """Sync a batch of sender contacts to HubSpot.

    Args:
        emails: List of (email, display_name) tuples from raw Gmail messages.
        own_emails: Emails belonging to the mailbox owner (skip these).
        search_fn: Callable that searches HubSpot by email; returns existing record dict or None.
        create_fn: Callable that creates a new HubSpot contact; returns the new contact id.
        update_fn: Callable that updates an existing HubSpot contact by id.

    Returns:
        List of SyncResult for each processed address.
    """
    seen: set[str] = set()
    results: list[SyncResult] = []

    for email, display_name in emails:
        email_key = email.lower().strip()

        if email_key in seen:
            continue
        seen.add(email_key)

        if should_skip(email_key, {e.lower() for e in own_emails}):
            results.append(SyncResult("Saltato", email_key, reason="automated/own sender"))
            continue

        firstname, lastname = parse_display_name(display_name)
        sender = Sender(email=email_key, firstname=firstname, lastname=lastname)

        existing = search_fn(email_key)

        if existing is None:
            props = build_hubspot_properties(sender, None)
            hs_id = create_fn(props)
            results.append(SyncResult("Creato", email_key, hubspot_id=str(hs_id)))
        else:
            hs_id = str(existing["id"])
            props = build_hubspot_properties(sender, existing)
            if props:
                update_fn(hs_id, props)
                results.append(SyncResult("Aggiornato", email_key, hubspot_id=hs_id))
            else:
                results.append(SyncResult("Ignorato", email_key, hubspot_id=hs_id,
                                          reason="all fields already populated"))

    return results
