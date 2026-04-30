import logging
import time
from dataclasses import dataclass, field

from .config import Config
from .gmail_client import GmailClient, HistoryExpiredError
from .hubspot_client import HubSpotClient
from .state import State

log = logging.getLogger(__name__)

_STATUS_ICON = {
    "created": "[+]",
    "updated": "[~]",
    "ignored": "[-]",
    "skipped": "[.]",
    "error":   "[!]",
}


@dataclass
class SyncResult:
    status: str          # created | updated | ignored | skipped | error
    email: str
    contact_id: str
    detail: str = ""


@dataclass
class SyncReport:
    results: list[SyncResult] = field(default_factory=list)

    def print_summary(self) -> None:
        if not self.results:
            print("  (nessuna nuova email da processare)")
            return
        print(f"\n{'STATO':<10} {'EMAIL':<42} {'HUBSPOT ID'}")
        print("-" * 72)
        for r in self.results:
            icon = _STATUS_ICON.get(r.status, "[?]")
            print(f"{icon} {r.status:<8} {r.email:<42} {r.contact_id or '-'}")
        total = len(self.results)
        created = sum(1 for r in self.results if r.status == "created")
        updated = sum(1 for r in self.results if r.status == "updated")
        ignored = sum(1 for r in self.results if r.status == "ignored")
        errors  = sum(1 for r in self.results if r.status == "error")
        print(f"\nTotale: {total}  |  Creati: {created}  Aggiornati: {updated}  Ignorati: {ignored}  Errori: {errors}\n")


class GmailHubSpotSync:
    def __init__(self, config: Config):
        self.config = config
        self.gmail = GmailClient(config.gmail_credentials_file, config.gmail_token_file)
        self.hubspot = HubSpotClient(config.hubspot_api_key, config.ignored_domains)
        self.state = State(config.state_file)

    # ------------------------------------------------------------------
    # Single poll cycle
    # ------------------------------------------------------------------

    def run_once(self, initial_sync: bool = False) -> SyncReport:
        report = SyncReport()

        if self.state.last_history_id is None:
            if initial_sync:
                log.info("Prima esecuzione con --initial-sync: processo le email recenti in INBOX")
                message_ids = self.gmail.list_inbox_message_ids(max_results=200)
            else:
                log.info("Prima esecuzione: salvo historyId corrente e attendo nuove email")
                self.state.last_history_id = self.gmail.get_current_history_id()
                return report
        else:
            try:
                message_ids = self.gmail.get_new_message_ids(self.state.last_history_id)
            except HistoryExpiredError:
                log.warning("HistoryId scaduto – recupero lista INBOX recente")
                message_ids = self.gmail.list_inbox_message_ids(max_results=200)

        # Update stored historyId before processing so we don't reprocess on error
        self.state.last_history_id = self.gmail.get_current_history_id()

        for msg_id in message_ids:
            result = self._process_message(msg_id)
            if result is not None:
                report.results.append(result)

        return report

    # ------------------------------------------------------------------
    # Continuous daemon
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        log.info("Avvio daemon Gmail → HubSpot (polling ogni %ds)", self.config.poll_interval)
        while True:
            try:
                report = self.run_once()
                report.print_summary()
            except KeyboardInterrupt:
                log.info("Interruzione ricevuta, uscita.")
                return
            except Exception as exc:
                log.error("Errore durante il ciclo di sync: %s", exc, exc_info=True)
            time.sleep(self.config.poll_interval)

    # ------------------------------------------------------------------
    # Per-message processing
    # ------------------------------------------------------------------

    def _process_message(self, message_id: str) -> SyncResult | None:
        if self.state.is_processed(message_id):
            return None

        try:
            msg = self.gmail.get_message_metadata(message_id)
            name, email = self.gmail.parse_sender(msg)
            subject = self.gmail.get_subject(msg)

            if not email:
                self.state.mark_processed(message_id)
                return None

            status, contact_id = self.hubspot.upsert_contact(name, email, subject)
            self.state.mark_processed(message_id)

            log.debug("%s %s → %s", _STATUS_ICON.get(status, "[?]"), email, contact_id or "—")
            return SyncResult(status=status, email=email, contact_id=contact_id)

        except Exception as exc:
            log.error("Errore processando messaggio %s: %s", message_id, exc)
            return SyncResult(status="error", email="", contact_id="", detail=str(exc))
