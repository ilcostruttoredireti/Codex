"""Core sync orchestration: Gmail inbox → HubSpot contacts."""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr

from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

INBOUND_LABEL = "Inbound Gmail"
CONTACT_SOURCE = "Gmail"
STATE_FILE = "state.json"

# Free / personal email domains — company name is not extracted from these.
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "mail.com", "gmx.com", "gmx.net", "yandex.com",
    "zoho.com", "libero.it", "virgilio.it", "alice.it", "tin.it",
    "fastwebnet.it", "tiscali.it",
}


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato" | "Errore"
    email: str = ""
    contact_id: str = ""
    reason: str = ""


@dataclass
class _State:
    processed_ids: set = field(default_factory=set)
    last_timestamp: int = 0  # Unix seconds of last successful poll


class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient):
        self.gmail = gmail
        self.hubspot = hubspot
        self._state = self._load_state()
        self._label_id: str | None = None

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> _State:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as fh:
                    data = json.load(fh)
                return _State(
                    processed_ids=set(data.get("processed_ids", [])),
                    last_timestamp=int(data.get("last_timestamp", 0)),
                )
            except (json.JSONDecodeError, KeyError):
                logger.warning("State file corrotto, ripartenza da zero.")
        return _State()

    def _save_state(self) -> None:
        with open(STATE_FILE, "w") as fh:
            json.dump(
                {
                    "processed_ids": list(self._state.processed_ids),
                    "last_timestamp": self._state.last_timestamp,
                },
                fh,
                indent=2,
            )

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sender(from_header: str) -> tuple[str, str]:
        """Return (display_name, email_address) from a 'From' header value."""
        name, addr = parseaddr(from_header)
        return name.strip(), addr.strip().lower()

    @staticmethod
    def _email_domain(email: str) -> str:
        parts = email.split("@")
        return parts[1] if len(parts) == 2 else ""

    @staticmethod
    def _domain_to_company(domain: str) -> str:
        """Best-effort company name from domain (empty for personal domains)."""
        if not domain or domain in _PERSONAL_DOMAINS:
            return ""
        # Strip subdomain and TLD; capitalise first segment.
        segment = domain.split(".")[0]
        return segment.capitalize()

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str]:
        parts = full_name.split(None, 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        if len(parts) == 1:
            return parts[0], ""
        return "", ""

    # ------------------------------------------------------------------
    # Gmail label
    # ------------------------------------------------------------------

    def _get_label_id(self) -> str | None:
        if self._label_id is None:
            self._label_id = self.gmail.get_or_create_label(INBOUND_LABEL)
        return self._label_id

    # ------------------------------------------------------------------
    # Single-message processing
    # ------------------------------------------------------------------

    def _process_message(self, message_id: str) -> SyncResult:
        if message_id in self._state.processed_ids:
            return SyncResult(status="Ignorato", reason="già processato")

        msg = self.gmail.get_message_metadata(message_id)
        if not msg:
            return SyncResult(status="Ignorato", reason="impossibile recuperare il messaggio")

        # Skip messages sent by us
        label_ids = msg.get("labelIds", [])
        if "SENT" in label_ids or "DRAFT" in label_ids:
            self._state.processed_ids.add(message_id)
            return SyncResult(status="Ignorato", reason="messaggio inviato/bozza")

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "").strip()
        subject = headers.get("Subject", "(nessun oggetto)")

        if not from_header:
            self._state.processed_ids.add(message_id)
            return SyncResult(status="Ignorato", reason="header From mancante")

        name, email_addr = self._parse_sender(from_header)

        if not email_addr or "@" not in email_addr:
            self._state.processed_ids.add(message_id)
            return SyncResult(status="Ignorato", email=from_header, reason="indirizzo email non valido")

        domain = self._email_domain(email_addr)
        company = self._domain_to_company(domain)
        firstname, lastname = self._split_name(name)

        # ---- HubSpot lookup ----
        existing = self.hubspot.find_contact_by_email(email_addr)

        if existing:
            contact_id = existing["id"]
            ep = existing.get("properties", {})

            # Populate only empty fields to avoid overwriting curated data.
            updates: dict[str, str] = {"hs_lead_source": CONTACT_SOURCE}
            if firstname and not ep.get("firstname"):
                updates["firstname"] = firstname
            if lastname and not ep.get("lastname"):
                updates["lastname"] = lastname
            if company and not ep.get("company"):
                updates["company"] = company

            self.hubspot.update_contact(contact_id, updates)
            status = "Aggiornato"
        else:
            new_props: dict[str, str] = {"email": email_addr, "hs_lead_source": CONTACT_SOURCE}
            if firstname:
                new_props["firstname"] = firstname
            if lastname:
                new_props["lastname"] = lastname
            if company:
                new_props["company"] = company
            if domain and domain not in _PERSONAL_DOMAINS:
                new_props["website"] = f"https://{domain}"

            created = self.hubspot.create_contact(new_props)
            if not created:
                self._state.processed_ids.add(message_id)
                return SyncResult(status="Errore", email=email_addr, reason="creazione contatto fallita")

            contact_id = created["id"]
            status = "Creato"

        # ---- Timeline note ----
        note_body = (
            f"<b>Email ricevuta tramite Gmail</b><br>"
            f"<b>Da:</b> {from_header}<br>"
            f"<b>Oggetto:</b> {subject}<br>"
            f"<b>Tag:</b> Inbound Gmail"
        )
        self.hubspot.create_note(contact_id, note_body)

        # ---- Gmail label ----
        label_id = self._get_label_id()
        if label_id:
            self.gmail.add_label(message_id, label_id)

        self._state.processed_ids.add(message_id)
        return SyncResult(status=status, email=email_addr, contact_id=str(contact_id))

    # ------------------------------------------------------------------
    # Public sync cycle
    # ------------------------------------------------------------------

    def run_once(self) -> list[SyncResult]:
        """Fetch new inbox messages and sync each sender to HubSpot."""
        after = self._state.last_timestamp or None
        messages = self.gmail.get_inbox_messages(after_timestamp=after)

        results: list[SyncResult] = []
        for stub in messages:
            result = self._process_message(stub["id"])
            results.append(result)

        # Advance the watermark so next poll only fetches newer messages.
        self._state.last_timestamp = int(datetime.now(timezone.utc).timestamp())
        self._save_state()

        return results
