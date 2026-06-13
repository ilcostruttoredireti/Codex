"""
Gmail → HubSpot Contact Sync

Monitors incoming Gmail emails and synchronizes sender contacts to HubSpot.
- Extracts sender info (email, name, company domain) from each thread
- Checks HubSpot for existing contacts (uses email as unique key)
- Creates new contacts or updates missing fields on existing ones
- Logs a timeline NOTE activity on every matched contact
- Skips automated senders (noreply, notifications, internal addresses)

Intended to run as a scheduled Claude Code routine via MCP tools.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "mailer-daemon.googlemail.com",
    "bounce.com",
    "amazonses.com",
    "sendgrid.net",
    "mailchimp.com",
    "sparkpostmail.com",
}

SKIP_LOCAL_PARTS = {
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "mailer-daemon",
    "postmaster",
    "notification",
    "notifications",
    "bounce",
    "bounces",
    "unsubscribe",
    "support",
    "help",
    "info",
    "admin",
    "newsletter",
}

GMAIL_TAG = "Inbound Gmail"
CONTACT_SOURCE = "Gmail"


@dataclass
class EmailSender:
    email: str
    name: Optional[str] = None
    company: Optional[str] = None

    @property
    def domain(self) -> str:
        return self.email.split("@")[1] if "@" in self.email else ""

    @property
    def local_part(self) -> str:
        return self.email.split("@")[0] if "@" in self.email else self.email

    def should_skip(self) -> bool:
        if self.domain in SKIP_DOMAINS:
            return True
        if self.local_part.lower() in SKIP_LOCAL_PARTS:
            return True
        return False


@dataclass
class SyncResult:
    email: str
    status: str  # "Creato" | "Aggiornato" | "Ignorato"
    contact_id: Optional[str] = None
    reason: Optional[str] = None


def parse_sender(raw_sender: str) -> EmailSender:
    """Parse a raw 'From' header into an EmailSender.

    Handles formats like:
      - "Name <email@domain.com>"
      - "email@domain.com"
    """
    match = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', raw_sender)
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip()
    else:
        name = None
        email = raw_sender.strip()

    sender = EmailSender(email=email, name=name)

    # Derive company from domain when domain is not a generic provider
    generic_domains = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                       "libero.it", "virgilio.it", "tiscali.it", "alice.it"}
    if sender.domain not in generic_domains:
        # Convert domain to readable company name (best-effort)
        company_raw = sender.domain.split(".")[0]
        sender.company = company_raw.replace("-", " ").replace("_", " ").title()

    return sender


def split_name(full_name: str) -> tuple[str, Optional[str]]:
    """Split 'First Last' into (firstname, lastname). Returns (full, None) if single token."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], None


def build_contact_properties(sender: EmailSender, existing: Optional[dict] = None) -> dict:
    """Build HubSpot contact property dict, filling only missing fields."""
    existing = existing or {}
    props: dict = {}

    if not existing.get("email"):
        props["email"] = sender.email

    if sender.name and not existing.get("firstname"):
        first, last = split_name(sender.name)
        props["firstname"] = first
        if last and not existing.get("lastname"):
            props["lastname"] = last

    if sender.company and not existing.get("company"):
        props["company"] = sender.company

    return props


def format_note(sender: EmailSender, subject: str, date: str) -> str:
    name_part = f" ({sender.name})" if sender.name else ""
    return (
        f"\U0001f4e7 Email ricevuta via Gmail (Inbound Gmail)\n"
        f"Da: {sender.email}{name_part}\n"
        f"Oggetto: {subject}\n"
        f"Data: {date}\n"
        f"Fonte: {CONTACT_SOURCE} | Tag: {GMAIL_TAG}"
    )


# ---------------------------------------------------------------------------
# The actual sync logic runs via Claude Code MCP calls (see routine prompt).
# The functions above are helpers for parsing / data shaping.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("This module is designed to run inside a Claude Code scheduled routine.")
    print("It exposes parsing helpers; the MCP orchestration is driven by the prompt.")
