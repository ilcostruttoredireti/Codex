import logging
from dataclasses import dataclass, field
from typing import Optional

from .contact_parser import parse_sender
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state_manager import StateManager

logger = logging.getLogger(__name__)

STATUS_CREATED = "created"
STATUS_UPDATED = "updated"
STATUS_SKIPPED = "skipped"
STATUS_ERROR = "error"

_STATUS_ICON = {
    STATUS_CREATED: "✅",
    STATUS_UPDATED: "🔄",
    STATUS_SKIPPED: "⏭️",
    STATUS_ERROR: "❌",
}


@dataclass
class SyncResult:
    status: str
    email: str
    contact_id: Optional[str]
    detail: str = ""


class SyncEngine:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state: StateManager,
        max_messages: int = 50,
    ):
        self._gmail = gmail
        self._hubspot = hubspot
        self._state = state
        self._max_messages = max_messages

    def run_once(self) -> list[SyncResult]:
        """Fetch new INBOX messages and sync senders to HubSpot."""
        results: list[SyncResult] = []
        new_history_id = self._gmail.get_current_history_id()
        start_id = self._state.last_history_id

        for stub in self._gmail.list_new_messages(
            start_history_id=start_id,
            max_results=self._max_messages,
        ):
            msg_id = stub["id"]
            if self._state.is_processed(msg_id):
                continue

            result = self._process_message(msg_id)
            self._state.mark_processed(msg_id)
            self._print_result(result)
            results.append(result)

        self._state.last_history_id = new_history_id
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_message(self, message_id: str) -> SyncResult:
        try:
            metadata = self._gmail.get_message_metadata(message_id)
        except Exception as exc:
            return SyncResult(STATUS_ERROR, "", None, f"Gmail fetch error: {exc}")

        from_header = _extract_header(metadata, "From")
        subject = _extract_header(metadata, "Subject") or ""

        if not from_header:
            return SyncResult(STATUS_SKIPPED, "", None, "missing From header")

        sender = parse_sender(from_header)
        if not sender:
            return SyncResult(
                STATUS_SKIPPED, from_header, None, "could not parse sender"
            )

        email = sender["email"]

        try:
            existing = self._hubspot.find_contact_by_email(email)
        except Exception as exc:
            return SyncResult(STATUS_ERROR, email, None, f"HubSpot lookup failed: {exc}")

        try:
            if existing:
                contact_id = existing["id"]
                detail = self._update_contact_if_needed(
                    contact_id, sender, existing.get("properties", {})
                )
                self._add_note(contact_id, email, subject)
                return SyncResult(STATUS_UPDATED, email, contact_id, detail)
            else:
                contact = self._hubspot.create_contact(
                    _build_properties(sender)
                )
                contact_id = contact["id"]
                self._add_note(contact_id, email, subject)
                return SyncResult(STATUS_CREATED, email, contact_id)
        except Exception as exc:
            return SyncResult(STATUS_ERROR, email, None, str(exc))

    def _update_contact_if_needed(
        self, contact_id: str, sender: dict, existing_props: dict
    ) -> str:
        updates: dict = {}
        if sender["first_name"] and not existing_props.get("firstname"):
            updates["firstname"] = sender["first_name"]
        if sender["last_name"] and not existing_props.get("lastname"):
            updates["lastname"] = sender["last_name"]
        if sender["company"] and not existing_props.get("company"):
            updates["company"] = sender["company"]
        if updates:
            self._hubspot.update_contact(contact_id, updates)
            return "aggiornato: " + ", ".join(updates)
        return "nessun campo da aggiornare"

    def _add_note(self, contact_id: str, email: str, subject: str) -> None:
        try:
            body = (
                f"📧 Email in arrivo da: {email}\n"
                f"Oggetto: {subject or '(nessuno)'}\n"
                f"Tag: Inbound Gmail"
            )
            self._hubspot.create_note(contact_id, body)
        except Exception as exc:
            logger.warning("Impossibile aggiungere nota per %s: %s", email, exc)

    @staticmethod
    def _print_result(r: SyncResult) -> None:
        icon = _STATUS_ICON.get(r.status, "?")
        parts = [
            f"{icon} [{r.status.upper():8}]",
            f"Email: {r.email or '-':45}",
            f"HubSpot ID: {r.contact_id or '-':12}",
        ]
        if r.detail:
            parts.append(r.detail)
        print("  ".join(parts))


def _build_properties(sender: dict) -> dict:
    props: dict = {
        "email": sender["email"],
        "lead_source_detail": "Gmail",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]
    return props


def _extract_header(metadata: dict, name: str) -> Optional[str]:
    for h in metadata.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return None
