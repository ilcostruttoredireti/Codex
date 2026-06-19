"""
Gmail → HubSpot Contact Sync Routine

Monitors incoming Gmail inbox messages, extracts unique senders, and syncs
them to HubSpot as contacts. Uses email as the deduplication key.

Runs as a scheduled Claude Code routine using Gmail and HubSpot MCP tools.
Each execution processes the last 48 hours of inbox messages.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "libero.it", "hotmail.com",
    "hotmail.it", "outlook.com", "outlook.it", "icloud.com", "tiscali.it",
    "virgilio.it", "alice.it", "tin.it", "aol.com",
}

AUTOMATED_SENDERS = {
    "facebookmail.com", "googlemail.com", "noreply.github.com",
    "bounce.linkedin.com", "notifications.google.com",
}


def extract_company_from_domain(domain: str) -> Optional[str]:
    """Derive a readable company name from the email domain."""
    if domain in PERSONAL_DOMAINS or domain in AUTOMATED_SENDERS:
        return None
    # Strip common subdomains (ufficiostampa., press., info., ...)
    parts = domain.split(".")
    # Use TLD-stripped base for a best-effort name
    name = parts[0] if len(parts) >= 2 else domain
    return name.replace("-", " ").replace("_", " ").title()


def parse_name(display_name: str) -> tuple[str, str]:
    """
    Split a display name into (firstname, lastname).
    Falls back to (display_name, "") for office/org names.
    """
    parts = display_name.strip().split(" ", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return display_name.strip(), ""


def is_automated(sender_email: str) -> bool:
    """Return True for automated/system senders to skip."""
    domain = sender_email.split("@")[-1].lower()
    if domain in AUTOMATED_SENDERS:
        return True
    local = sender_email.split("@")[0].lower()
    return any(tok in local for tok in ("noreply", "no-reply", "mailer-daemon",
                                        "postmaster", "bounce", "notification"))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Sender:
    email: str
    display_name: str = ""
    domain: str = field(init=False)

    def __post_init__(self):
        self.domain = self.email.split("@")[-1].lower()

    @property
    def firstname(self) -> str:
        return parse_name(self.display_name)[0] if self.display_name else ""

    @property
    def lastname(self) -> str:
        return parse_name(self.display_name)[1] if self.display_name else ""

    @property
    def company(self) -> Optional[str]:
        return extract_company_from_domain(self.domain)


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str   # "Creato" | "Aggiornato" | "Ignorato"
    reason: str = ""


# ---------------------------------------------------------------------------
# Sync logic (executed by Claude via MCP tools — see inline comments)
# ---------------------------------------------------------------------------

def run_sync(days_back: int = 2) -> list[SyncResult]:
    """
    Main entry point.  In practice this function is executed by Claude Code
    using the Gmail and HubSpot MCP tools:

    Step 1 — Gmail: search_threads(query="in:inbox -from:me newer_than:{days_back}d")
    Step 2 — For threads with ambiguous snippets: get_thread(messageFormat=METADATA_ONLY)
    Step 3 — Deduplicate senders; skip self-emails and automated senders
    Step 4 — HubSpot: search_crm_objects(objectType="contacts", filter email IN [...])
    Step 5 — For each sender:
              • Exists → check for missing fields → manage_crm_objects(updateRequest)
              • Missing → manage_crm_objects(createRequest) with:
                  email, firstname, lastname, company,
                  hs_lead_source="Gmail",
                  notes_last_updated=<today>
    Step 6 — Return SyncResult list and push notification
    """
    raise NotImplementedError(
        "This function documents the routine logic. "
        "Execution happens via Claude Code MCP tool calls."
    )


# ---------------------------------------------------------------------------
# HubSpot contact builder
# ---------------------------------------------------------------------------

def build_hubspot_properties(sender: Sender) -> dict:
    """Return the property dict to use for HubSpot create/update."""
    props: dict = {"email": sender.email}
    if sender.firstname:
        props["firstname"] = sender.firstname
    if sender.lastname:
        props["lastname"] = sender.lastname
    if sender.company:
        props["company"] = sender.company
    props["hs_lead_status"] = "NEW"
    return props


# ---------------------------------------------------------------------------
# Skip rules (applied before any HubSpot calls)
# ---------------------------------------------------------------------------

SKIP_RULES = [
    (lambda s, own: s.email == own, "mittente coincide con l'utente"),
    (lambda s, own: is_automated(s.email), "mittente automatico/sistema"),
    (lambda s, own: s.domain in PERSONAL_DOMAINS and not s.display_name,
     "email personale senza nome visualizzato"),
]


def should_skip(sender: Sender, owner_email: str) -> Optional[str]:
    """Return skip reason string, or None if the sender should be processed."""
    for rule, reason in SKIP_RULES:
        if rule(sender, owner_email):
            return reason
    return None
