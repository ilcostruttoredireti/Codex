import re
from email.utils import parseaddr
from typing import Optional

from config import GENERIC_DOMAINS, IGNORED_SENDER_PATTERNS
from hubspot_client import HubSpotClient, HubSpotError


class SyncResult:
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"

    def __init__(
        self,
        status: str,
        email: Optional[str] = None,
        hubspot_id: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        self.status = status
        self.email = email
        self.hubspot_id = hubspot_id
        self.reason = reason

    def __str__(self) -> str:
        icon = {
            SyncResult.CREATED: "✓",
            SyncResult.UPDATED: "↻",
            SyncResult.IGNORED: "—",
            SyncResult.ERROR: "✗",
        }.get(self.status, "?")
        parts = [f"{icon} [{self.status}]", self.email or "N/A", f"→ ID: {self.hubspot_id or '-'}"]
        if self.reason:
            parts.append(f"({self.reason})")
        return "  " + "  ".join(parts)


class ContactSync:
    def __init__(self, hubspot: HubSpotClient):
        self.hubspot = hubspot

    # ------------------------------------------------------------------ #
    # Parsing helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_from_header(from_header: str) -> tuple[Optional[str], Optional[str]]:
        """Return (email, display_name) from a raw From header."""
        name, addr = parseaddr(from_header)
        addr = addr.lower().strip() if addr else None
        name = name.strip() or None
        return addr, name

    @staticmethod
    def _infer_name_from_address(local_part: str) -> str:
        """john.doe  →  John Doe"""
        parts = re.split(r"[._\-+]", local_part)
        return " ".join(p.capitalize() for p in parts if p)

    @staticmethod
    def _split_full_name(full_name: str) -> tuple[Optional[str], Optional[str]]:
        parts = full_name.strip().split(None, 1)
        return (parts[0] if parts else None), (parts[1] if len(parts) > 1 else None)

    @staticmethod
    def _company_from_domain(domain: str) -> Optional[str]:
        if domain in GENERIC_DOMAINS:
            return None
        root = domain.split(".")[0]
        return root.capitalize() if root else None

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def process_email(
        self,
        from_header: str,
        subject: str = "",
        date: str = "",
    ) -> SyncResult:
        """
        Sync the sender of one Gmail message to HubSpot.

        Returns a SyncResult describing what happened.
        """
        email, display_name = self._parse_from_header(from_header)

        if not email or "@" not in email:
            return SyncResult(SyncResult.IGNORED, reason="Nessuna email mittente valida")

        email_lower = email.lower()

        if any(pat in email_lower for pat in IGNORED_SENDER_PATTERNS):
            return SyncResult(SyncResult.IGNORED, email=email, reason="Mittente automatico/no-reply")

        # Resolve name
        if not display_name:
            local = email.split("@")[0]
            display_name = self._infer_name_from_address(local)

        firstname, lastname = self._split_full_name(display_name)
        domain = email.split("@")[1]
        company = self._company_from_domain(domain)

        try:
            existing = self.hubspot.search_contact_by_email(email)
        except HubSpotError as exc:
            return SyncResult(SyncResult.ERROR, email=email, reason=str(exc))

        if existing:
            return self._handle_existing(existing, firstname, lastname, company, from_header, subject, date)
        else:
            return self._handle_new(email, firstname, lastname, company, from_header, subject, date)

    # ------------------------------------------------------------------ #
    # Internal handlers                                                    #
    # ------------------------------------------------------------------ #

    def _handle_existing(
        self,
        contact: dict,
        firstname: Optional[str],
        lastname: Optional[str],
        company: Optional[str],
        from_header: str,
        subject: str,
        date: str,
    ) -> SyncResult:
        contact_id = contact["id"]
        props = contact.get("properties", {})

        updates: dict[str, str] = {}
        if not props.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not props.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not props.get("company") and company:
            updates["company"] = company

        try:
            if updates:
                self.hubspot.update_contact(contact_id, updates)
            self._add_email_note(contact_id, from_header, subject, date)
        except HubSpotError as exc:
            return SyncResult(SyncResult.ERROR, email=props.get("email"), hubspot_id=contact_id, reason=str(exc))

        return SyncResult(SyncResult.UPDATED, email=props.get("email"), hubspot_id=contact_id)

    def _handle_new(
        self,
        email: str,
        firstname: Optional[str],
        lastname: Optional[str],
        company: Optional[str],
        from_header: str,
        subject: str,
        date: str,
    ) -> SyncResult:
        properties: dict[str, str] = {
            "email": email,
            "hs_lead_status": "NEW",
        }
        if firstname:
            properties["firstname"] = firstname
        if lastname:
            properties["lastname"] = lastname
        if company:
            properties["company"] = company

        try:
            result = self.hubspot.create_contact(properties)
            contact_id = result["id"]
            self._add_email_note(contact_id, from_header, subject, date, is_new=True)
        except HubSpotError as exc:
            return SyncResult(SyncResult.ERROR, email=email, reason=str(exc))

        return SyncResult(SyncResult.CREATED, email=email, hubspot_id=contact_id)

    def _add_email_note(
        self,
        contact_id: str,
        from_header: str,
        subject: str,
        date: str,
        is_new: bool = False,
    ) -> None:
        action = "Contatto creato" if is_new else "Email ricevuta"
        body = (
            f"[Inbound Gmail] {action}\n"
            f"Da: {from_header}\n"
            f"Oggetto: {subject or '(nessun oggetto)'}\n"
            f"Data: {date or '(sconosciuta)'}\n"
            f"Fonte contatto: Gmail"
        )
        try:
            self.hubspot.create_note_on_contact(contact_id, body)
        except HubSpotError:
            # Note creation is best-effort; don't fail the whole sync
            pass
