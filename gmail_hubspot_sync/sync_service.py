import logging

from .contact_parser import parse_from_header
from .gmail_client import GmailClient, EmailMessage
from .hubspot_client import HubSpotClient, SyncOutcome, SyncResult

logger = logging.getLogger(__name__)


class SyncService:
    """Orchestratore: legge email Gmail → sincronizza contatti HubSpot."""

    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        processed_label_name: str,
        enable_activity_note: bool = True,
    ):
        self._gmail = gmail
        self._hubspot = hubspot
        self._processed_label_name = processed_label_name
        self._enable_activity_note = enable_activity_note
        self._processed_label_id: str | None = None

    def setup(self) -> None:
        """Inizializzazione: autentica Gmail e prepara label."""
        self._gmail.authenticate()
        self._processed_label_id = self._gmail.get_or_create_label(
            self._processed_label_name
        )
        logger.info(
            f"Label di tracciamento: '{self._processed_label_name}' "
            f"(ID: {self._processed_label_id})"
        )

    def run_once(self) -> list[SyncOutcome]:
        """
        Esegue un singolo ciclo di sync.
        Restituisce la lista degli esiti per ogni email processata.
        """
        messages = self._gmail.get_unprocessed_inbox_messages(
            processed_label_id=self._processed_label_id
        )

        if not messages:
            logger.debug("Nessuna nuova email da processare.")
            return []

        logger.info(f"Trovate {len(messages)} nuove email da processare.")
        outcomes: list[SyncOutcome] = []

        for msg in messages:
            outcome = self._process_message(msg)
            outcomes.append(outcome)
            self._log_outcome(outcome, msg)
            # Marca il messaggio come processato indipendentemente dall'esito
            self._gmail.mark_as_processed(msg.message_id, self._processed_label_id)

        return outcomes

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_message(self, msg: EmailMessage) -> SyncOutcome:
        contact_data = parse_from_header(msg.from_header)

        if contact_data is None:
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                contact_email=msg.from_header,
                contact_id=None,
                detail="impossibile parsare header From",
            )

        existing = self._hubspot.find_contact_by_email(contact_data.email)

        if existing is None:
            outcome = self._hubspot.create_contact(contact_data)
        else:
            outcome = self._hubspot.update_contact(
                contact_id=existing.id,
                contact_data=contact_data,
                existing=existing,
            )

        # Nota attività (opzionale, non bloccante)
        if (
            self._enable_activity_note
            and outcome.contact_id
            and outcome.result in (SyncResult.CREATED, SyncResult.UPDATED)
        ):
            self._hubspot.create_activity_note(
                contact_id=outcome.contact_id,
                subject=msg.subject,
                email_date=msg.date,
            )

        return outcome

    @staticmethod
    def _log_outcome(outcome: SyncOutcome, msg: EmailMessage) -> None:
        icon = {"Creato": "✚", "Aggiornato": "↻", "Ignorato": "–"}.get(
            outcome.result.value, "?"
        )
        logger.info(
            f"{icon} {outcome} | Oggetto: \"{msg.subject[:60]}\""
        )
