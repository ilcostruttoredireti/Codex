"""
Core sync logic: Gmail sender → HubSpot contact (create / update / skip).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from hubspot_client import HubSpotClient

# Email addresses/domains that should never become contacts
_SKIP_PREFIXES = (
    "noreply@",
    "no-reply@",
    "donotreply@",
    "do-not-reply@",
    "mailer-daemon@",
    "postmaster@",
    "bounce@",
    "bounces@",
    "notifications@",
    "notify@",
    "alerts@",
    "support@",
    "help@",
    "info@",
    "newsletter@",
    "unsubscribe@",
    "reply@",
    "automated@",
)

# Domains belonging to personal / free email providers — no company name extracted
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "hotmail.co.uk",
    "outlook.com", "outlook.it",
    "live.com", "live.it",
    "msn.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com",
    "protonmail.com", "proton.me",
    "mail.com", "gmx.com", "gmx.net",
    "yandex.com", "yandex.ru",
    "zoho.com",
    "libero.it", "virgilio.it", "tin.it", "alice.it",
    "tiscali.it", "fastwebnet.it", "katamail.com", "email.it",
    "tutanota.com", "tutanota.de",
}


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: str | None
    reason: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status}]", f"email={self.email}"]
        if self.hubspot_id:
            parts.append(f"id={self.hubspot_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return "  ".join(parts)


def _should_skip(email: str) -> bool:
    lower = email.lower()
    return any(lower.startswith(p) or lower == p.rstrip("@") for p in _SKIP_PREFIXES)


def _parse_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' → ('First', 'Last'). Returns ('', '') if blank."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """
    Returns a guessed company name from the domain, or '' for personal providers.
    E.g. 'acme.com' → 'Acme', 'mail.google.com' → 'Google'.
    """
    if domain in _PERSONAL_DOMAINS:
        return ""
    # Take the second-to-last label (drops TLD and www / mail subdomains)
    labels = domain.split(".")
    if len(labels) >= 2:
        return labels[-2].capitalize()
    return domain.capitalize()


def _build_properties(
    email: str,
    first_name: str,
    last_name: str,
    company: str,
) -> dict:
    props: dict[str, str] = {"email": email, "lifecyclestage": "lead"}
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if company:
        props["company"] = company
    return props


def _missing_fields(existing_props: dict, candidate: dict) -> dict:
    """
    Returns only fields from `candidate` that are absent (empty/None) in
    `existing_props`, so we never overwrite data already in HubSpot.
    """
    updates: dict[str, str] = {}
    for key, value in candidate.items():
        if key == "email":
            continue  # never overwrite the key field
        if not existing_props.get(key):
            updates[key] = value
    return updates


def process_email(
    sender: dict,
    hubspot: HubSpotClient,
    add_timeline: bool = True,
) -> SyncResult:
    """
    Processes one email sender dict (from GmailClient.get_message_sender)
    and syncs it to HubSpot.

    Returns a SyncResult with status Creato / Aggiornato / Ignorato.
    """
    email = sender["email"]

    if _should_skip(email):
        return SyncResult("Ignorato", email, None, "mittente automatico")

    first_name, last_name = _parse_name(sender.get("name", ""))
    company = _company_from_domain(sender.get("domain", ""))
    candidate = _build_properties(email, first_name, last_name, company)

    existing = hubspot.find_contact_by_email(email)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    note_body = (
        f"📧 Email ricevuta — {now_str}\n"
        f"Mittente: {sender.get('name') or email}\n"
        f"Oggetto: {sender.get('subject', '')}\n"
        f"Tag: Inbound Gmail\n"
        f"Fonte: Gmail"
    )

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        updates = _missing_fields(existing_props, candidate)

        if updates:
            hubspot.update_contact(contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"

        if add_timeline:
            hubspot.add_note_to_contact(contact_id, note_body)

        return SyncResult(status, email, contact_id)

    else:
        new_contact = hubspot.create_contact(candidate)
        contact_id = new_contact["id"]

        if add_timeline:
            hubspot.add_note_to_contact(contact_id, note_body)

        return SyncResult("Creato", email, contact_id)
