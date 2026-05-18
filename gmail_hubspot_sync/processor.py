import logging
import time

logger = logging.getLogger(__name__)

# Compound TLDs for smarter company extraction
_COMPOUND_TLDS = {
    "co.uk", "co.jp", "com.br", "com.au", "co.in",
    "co.nz", "co.za", "org.uk", "net.uk", "co.kr",
}

# Free email providers — domain not useful as company name
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "aol.com", "icloud.com", "protonmail.com",
    "mail.com", "yandex.com", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "tin.it",
}


class ContactProcessor:
    def __init__(self, gmail_client, hubspot_client, state_manager, max_per_run: int = 50):
        self.gmail = gmail_client
        self.hubspot = hubspot_client
        self.state = state_manager
        self.max_per_run = max_per_run

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def process_new_emails(self):
        last_poll = self.state.get_config("last_poll_timestamp")

        if last_poll:
            after_epoch = int(last_poll)
        else:
            # First run: look back 1 hour only
            after_epoch = int(time.time()) - 3600

        current_time = int(time.time())
        messages = self.gmail.get_messages(
            after_epoch=after_epoch, max_results=self.max_per_run
        )

        if not messages:
            logger.debug("Nessun nuovo messaggio")
            self.state.set_config("last_poll_timestamp", current_time)
            return

        logger.info("Trovati %d messaggi da verificare", len(messages))

        for stub in messages:
            msg_id = stub["id"]

            if self.state.is_processed(msg_id):
                continue

            detail = self.gmail.get_message_detail(msg_id)
            if not detail:
                continue

            sender = self.gmail.parse_sender(detail)
            if not sender:
                self.state.mark_processed(msg_id, "", "IGNORATO")
                continue

            status, contact_id = self._sync_contact(sender)
            self.state.mark_processed(msg_id, sender["email"], status, contact_id)

            logger.info(
                "Stato: %-9s | Email: %-35s | ID HubSpot: %s",
                status,
                sender["email"],
                contact_id or "N/A",
            )

        self.state.set_config("last_poll_timestamp", current_time)

    # ------------------------------------------------------------------
    # Sync logic
    # ------------------------------------------------------------------

    def _sync_contact(self, sender: dict) -> tuple[str, str | None]:
        email = sender["email"]
        existing = self.hubspot.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            existing_props = existing.get("properties", {})
            updates = self._build_update_properties(sender, existing_props)
            if updates:
                self.hubspot.update_contact(contact_id, updates)
            self._add_email_note(contact_id, sender, is_new=False)
            return "AGGIORNATO", contact_id

        props = self._build_create_properties(sender)
        created = self.hubspot.create_contact(props)
        if not created:
            return "ERRORE", None

        contact_id = created["id"]
        self._add_email_note(contact_id, sender, is_new=True)
        return "CREATO", contact_id

    # ------------------------------------------------------------------
    # Property builders
    # ------------------------------------------------------------------

    def _build_create_properties(self, sender: dict) -> dict:
        first, last = self._split_name(sender["name"])
        company = self._extract_company(sender["domain"])

        props: dict = {
            "email": sender["email"],
            "hs_lead_source": "OTHER",
        }
        if first:
            props["firstname"] = first
        if last:
            props["lastname"] = last
        if company:
            props["company"] = company

        return props

    def _build_update_properties(self, sender: dict, existing: dict) -> dict:
        first, last = self._split_name(sender["name"])
        company = self._extract_company(sender["domain"])

        updates: dict = {}
        if first and not existing.get("firstname"):
            updates["firstname"] = first
        if last and not existing.get("lastname"):
            updates["lastname"] = last
        if company and not existing.get("company"):
            updates["company"] = company

        return updates

    # ------------------------------------------------------------------
    # Note / activity
    # ------------------------------------------------------------------

    def _add_email_note(self, contact_id: str, sender: dict, is_new: bool):
        label = "Nuovo contatto — Inbound Gmail" if is_new else "Email ricevuta — Inbound Gmail"
        note = (
            f"[{label}]\n\n"
            f"Da:      {sender['email']}\n"
            f"Nome:    {sender['name'] or 'N/D'}\n"
            f"Oggetto: {sender.get('subject', 'N/D')}\n"
            f"Data:    {sender.get('date', 'N/D')}\n\n"
            f"Fonte contatto: Gmail\n"
            f"Tag: Inbound Gmail"
        )
        self.hubspot.add_note_to_contact(
            contact_id, note, timestamp_ms=sender.get("internal_date")
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _split_name(full_name: str) -> tuple[str, str]:
        if not full_name:
            return "", ""
        parts = full_name.strip().split()
        if not parts:
            return "", ""
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], " ".join(parts[1:])

    @staticmethod
    def _extract_company(domain: str) -> str:
        if not domain or domain in _GENERIC_DOMAINS:
            return ""

        parts = domain.split(".")

        # Handle compound TLDs (e.g. co.uk)
        if len(parts) >= 3 and ".".join(parts[-2:]) in _COMPOUND_TLDS:
            return parts[-3].capitalize()

        if len(parts) >= 2:
            return parts[-2].capitalize()

        return domain.capitalize()
