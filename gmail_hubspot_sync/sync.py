import logging
from dataclasses import dataclass
from typing import Literal, Optional

from . import config, hubspot

logger = logging.getLogger(__name__)

Status = Literal["creato", "aggiornato", "ignorato"]


@dataclass
class SyncResult:
    status: Status
    email: str
    contact_id: Optional[str]
    reason: str = ""

    def __str__(self) -> str:
        icon = {"creato": "✅", "aggiornato": "🔄", "ignorato": "⏭️"}.get(self.status, "?")
        suffix = f" ({self.reason})" if self.reason else ""
        return (
            f"{icon} {self.status.upper():10} | "
            f"Email: {self.email:45} | "
            f"HubSpot ID: {self.contact_id or 'N/A'}{suffix}"
        )


def _should_ignore(email: str) -> Optional[str]:
    """Return a reason string if this sender should be skipped, else None."""
    if not email or "@" not in email:
        return "indirizzo non valido"
    for pattern in config.IGNORED_SENDER_PATTERNS:
        if pattern in email.lower():
            return f"indirizzo sistema ({pattern})"
    return None


def _build_note_body(sender: dict, is_new: bool) -> str:
    label = "Nuovo contatto" if is_new else "Email ricevuta"
    lines = [
        f"[Inbound Gmail] {label}",
        f"Tags: Inbound Gmail",
        f"Mittente: {sender['full_name'] or sender['email']}",
        f"Email: {sender['email']}",
    ]
    if sender.get("domain"):
        lines.append(f"Dominio: {sender['domain']}")
    if sender.get("subject"):
        lines.append(f"Oggetto: {sender['subject']}")
    if sender.get("date"):
        lines.append(f"Data: {sender['date']}")
    return "\n".join(lines)


def sync_sender(sender: dict) -> SyncResult:
    """
    Sync a single Gmail sender to HubSpot.

    Logic:
    - Skip system/noreply addresses → Ignorato
    - Contact exists → update only blank fields, add timeline note → Aggiornato
    - Contact missing → create with all available fields, add note → Creato
    """
    email = sender["email"]

    ignore_reason = _should_ignore(email)
    if ignore_reason:
        return SyncResult(status="ignorato", email=email, contact_id=None, reason=ignore_reason)

    existing = hubspot.search_contact_by_email(email)

    if existing:
        contact_id: str = existing["id"]
        props: dict = existing.get("properties", {})

        # Build patch with only currently-blank fields
        updates: dict = {}
        if not props.get("firstname") and sender.get("firstname"):
            updates["firstname"] = sender["firstname"]
        if not props.get("lastname") and sender.get("lastname"):
            updates["lastname"] = sender["lastname"]
        if not props.get("company") and sender.get("company"):
            updates["company"] = sender["company"]

        if updates:
            hubspot.update_contact(contact_id, updates)
            logger.debug("Aggiornati campi %s per contatto %s", list(updates), contact_id)

        # Always log the inbound email as a timeline note
        hubspot.create_note(
            body=_build_note_body(sender, is_new=False),
            contact_id=contact_id,
        )

        return SyncResult(status="aggiornato", email=email, contact_id=contact_id)

    else:
        # Build create payload
        properties: dict = {"email": email}
        if sender.get("firstname"):
            properties["firstname"] = sender["firstname"]
        if sender.get("lastname"):
            properties["lastname"] = sender["lastname"]
        if sender.get("company"):
            properties["company"] = sender["company"]

        contact = hubspot.create_contact(properties)
        contact_id = contact["id"]

        hubspot.create_note(
            body=_build_note_body(sender, is_new=True),
            contact_id=contact_id,
        )

        return SyncResult(status="creato", email=email, contact_id=contact_id)
