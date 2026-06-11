import logging
from typing import List, Tuple

from .config import Config
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .models import ContactResult, EmailSender
from .state import SyncState

logger = logging.getLogger(__name__)


class GmailHubSpotSync:
    def __init__(self, config: Config):
        self.config = config
        self.gmail = GmailClient(config.gmail_credentials_file, config.gmail_token_file)
        self.hubspot = HubSpotClient(config.hubspot_api_token)

    # ------------------------------------------------------------------
    # Core contact sync logic
    # ------------------------------------------------------------------

    def _sync_sender(self, sender: EmailSender) -> ContactResult:
        email = sender.email

        if self.config.skip_personal_domains and sender.domain in self.config.personal_domains:
            return ContactResult(
                status="ignored",
                email=email,
                contact_id=None,
                reason=f"dominio personale: {sender.domain}",
            )

        existing = self.hubspot.search_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            existing_props = existing.get("properties", {})
            updates: dict = {}

            if sender.name and not existing_props.get("firstname"):
                first, last = HubSpotClient.parse_name(sender.name)
                if first:
                    updates["firstname"] = first
                if last:
                    updates["lastname"] = last

            company = HubSpotClient.company_from_domain(sender.domain, self.config.personal_domains)
            if company and not existing_props.get("company"):
                updates["company"] = company

            if updates:
                self.hubspot.update_contact(contact_id, updates)
                status = "updated"
            else:
                status = "ignored"

            note = (
                f"Email inbound da Gmail\n"
                f"Tag: Inbound Gmail\n"
                f"Oggetto: {sender.subject}\n"
                f"Data: {sender.date}"
            )
            self.hubspot.add_note_to_contact(contact_id, note)

            return ContactResult(status=status, email=email, contact_id=contact_id)

        # --- Create new contact ---
        props: dict = {"email": email}

        if sender.name:
            first, last = HubSpotClient.parse_name(sender.name)
            if first:
                props["firstname"] = first
            if last:
                props["lastname"] = last

        company = HubSpotClient.company_from_domain(sender.domain, self.config.personal_domains)
        if company:
            props["company"] = company

        new_contact = self.hubspot.create_contact(props)
        contact_id = new_contact["id"]

        note = (
            f"Contatto creato automaticamente da Gmail\n"
            f"Tag: Inbound Gmail\n"
            f"Fonte: Gmail\n"
            f"Oggetto prima email: {sender.subject}\n"
            f"Data: {sender.date}"
        )
        self.hubspot.add_note_to_contact(contact_id, note)

        return ContactResult(status="created", email=email, contact_id=contact_id)

    # ------------------------------------------------------------------
    # Sync cycle
    # ------------------------------------------------------------------

    def run_cycle(self, state: SyncState) -> Tuple[SyncState, List[ContactResult]]:
        """Fetch new Gmail messages and sync senders to HubSpot."""
        results: List[ContactResult] = []

        if state.last_history_id is None:
            logger.info("First run — recupero le 50 email più recenti dalla posta in arrivo")
            messages = self.gmail.get_recent_inbox_messages(max_results=50)
        else:
            messages = self.gmail.get_new_messages_since(state.last_history_id)
            logger.debug(
                "%d nuovo/i messaggio/i trovato/i dall'historyId %s",
                len(messages), state.last_history_id,
            )

        new_history_id = self.gmail.get_current_history_id()

        seen_ids = set(state.processed_message_ids)

        for msg in messages:
            msg_id = msg.get("id", "")
            if not msg_id or msg_id in seen_ids:
                continue

            seen_ids.add(msg_id)

            sender = self.gmail.get_message_sender(msg_id)
            if sender is None:
                continue

            try:
                result = self._sync_sender(sender)
                results.append(result)
                _log_result(result)
            except Exception as exc:
                logger.error("Errore durante la sincronizzazione di %s: %s", sender.email, exc)

        # Keep only the most recent 2000 IDs to bound disk/memory usage
        state.processed_message_ids = list(seen_ids)[-2000:]
        state.last_history_id = new_history_id

        return state, results


def _log_result(result: ContactResult) -> None:
    suffix = f" — {result.reason}" if result.reason else ""
    logger.info(
        "[%-8s] %-40s  ID HubSpot: %s%s",
        result.status.upper(),
        result.email,
        result.contact_id or "N/A",
        suffix,
    )
