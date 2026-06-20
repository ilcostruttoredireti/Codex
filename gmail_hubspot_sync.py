"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot.
Runs as a Claude Code scheduled routine via MCP tools.

For each inbound email:
  - Extracts: email, name, company domain
  - Checks HubSpot: create if new, update if fields are missing
  - Sets lead source = "Gmail", tag = "Inbound Gmail"
  - Deduplicates on email address
"""

import re
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"


@dataclass
class SenderContact:
    email: str
    display_name: str = ""
    firstname: str = ""
    lastname: str = ""
    company: str = ""

    def __post_init__(self):
        if self.display_name and not self.firstname:
            self._parse_display_name()
        if not self.company and self.email:
            self._infer_company_from_domain()

    def _parse_display_name(self):
        name = self.display_name.strip()
        # Strip known role prefixes for splitting
        prefixes = ["Ufficio Stampa", "Ufficio stampa"]
        role = ""
        for p in prefixes:
            if name.startswith(p):
                role = p
                name = name[len(p):].strip(" -–")
                break

        parts = name.split()
        if role:
            self.firstname = role
            self.lastname = " ".join(parts) if parts else ""
            if not self.company and parts:
                self.company = " ".join(parts)
        elif len(parts) == 1:
            self.firstname = parts[0]
        elif len(parts) >= 2:
            self.firstname = parts[0]
            self.lastname = " ".join(parts[1:])

    def _infer_company_from_domain(self):
        personal_domains = {
            "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "libero.it", "virgilio.it", "alice.it", "tiscali.it",
        }
        domain = self.email.split("@")[-1].lower()
        if domain not in personal_domains:
            # Humanise the domain: drop TLD and capitalise
            parts = domain.split(".")
            self.company = parts[0].replace("-", " ").title()


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    display_name: str = ""

    def __str__(self):
        return f"{self.status.value:10} | {self.email:55} | {self.hubspot_id or 'N/A':15} | {self.display_name}"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_FROM_PATTERN = re.compile(
    r'Da\s+"([^"]+)"\s+([\w.\-+]+@[\w.\-]+)|'   # Da "Name" email@domain
    r'Da\s+([\w.\-+]+@[\w.\-]+)|'                # Da email@domain (no name)
    r'Da:\s+([^<\n]+?)\s+<([\w.\-+]+@[\w.\-]+)>|'  # Da: Name <email>
    r'from:\s*([\w.\-+]+@[\w.\-]+)',              # forwarded from:
    re.IGNORECASE,
)

_FWD_PATTERN = re.compile(
    r'Da:\s+([^<\n]+?)\s+<([\w.\-+]+@[\w.\-]+)>',
    re.IGNORECASE,
)


def extract_sender(snippet: str) -> Optional[SenderContact]:
    """Return a SenderContact from a Gmail thread snippet, or None."""
    # Try forwarded-message form first
    for m in _FWD_PATTERN.finditer(snippet):
        name, email = m.group(1).strip(), m.group(2).strip()
        return SenderContact(email=email, display_name=name)

    m = _FROM_PATTERN.search(snippet)
    if not m:
        return None

    groups = m.groups()
    if groups[0] and groups[1]:          # Da "Name" email
        return SenderContact(email=groups[1], display_name=groups[0])
    if groups[2]:                        # Da email (bare)
        return SenderContact(email=groups[2])
    if groups[3] and groups[4]:          # Da: Name <email>
        return SenderContact(email=groups[4], display_name=groups[3])
    if groups[5]:                        # from: email
        return SenderContact(email=groups[5])
    return None


# ---------------------------------------------------------------------------
# HubSpot operations (called by the Claude routine via MCP)
# ---------------------------------------------------------------------------

def build_create_properties(contact: SenderContact) -> dict:
    props = {
        "email": contact.email,
        "hs_lead_source": "Gmail",
    }
    if contact.firstname:
        props["firstname"] = contact.firstname
    if contact.lastname:
        props["lastname"] = contact.lastname
    if contact.company:
        props["company"] = contact.company
    return props


def build_update_properties(contact: SenderContact, existing: dict) -> dict:
    """Return only the fields that are currently blank in HubSpot."""
    props = {}
    if not existing.get("firstname") and contact.firstname:
        props["firstname"] = contact.firstname
    if not existing.get("lastname") and contact.lastname:
        props["lastname"] = contact.lastname
    if not existing.get("company") and contact.company:
        props["company"] = contact.company
    if not existing.get("hs_lead_source"):
        props["hs_lead_source"] = "Gmail"
    return props


# ---------------------------------------------------------------------------
# Entry point used by the Claude routine
# ---------------------------------------------------------------------------

def process_threads(threads: list[dict]) -> tuple[list[SyncResult], list[SenderContact]]:
    """
    Phase 1: parse all threads and return unique contacts.
    Returns (placeholder_results, unique_contacts).
    Deduplication is by email address.
    """
    seen: dict[str, SenderContact] = {}
    skipped_results = []

    for thread in threads:
        for msg in thread.get("messages", []):
            snippet = msg.get("snippet", "")
            contact = extract_sender(snippet)
            if not contact:
                continue
            email = contact.email.lower()
            # Skip self-sent / notification emails
            if not email or "@" not in email:
                continue
            if email in seen:
                skipped_results.append(
                    SyncResult(SyncStatus.SKIPPED, email,
                               display_name="(duplicato nel batch)")
                )
                continue
            seen[email] = contact

    return skipped_results, list(seen.values())


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(results: list[SyncResult]) -> str:
    header = f"{'Stato':10} | {'Email':55} | {'HubSpot ID':15} | Nome"
    sep = "-" * 110
    rows = "\n".join(str(r) for r in results)
    totals = {s: sum(1 for r in results if r.status == s) for s in SyncStatus}
    summary = (
        f"\nRiepilogo: {totals[SyncStatus.CREATED]} creati | "
        f"{totals[SyncStatus.UPDATED]} aggiornati | "
        f"{totals[SyncStatus.SKIPPED]} ignorati | "
        f"Totale processati: {len(results)}"
    )
    return f"{header}\n{sep}\n{rows}\n{sep}{summary}"
