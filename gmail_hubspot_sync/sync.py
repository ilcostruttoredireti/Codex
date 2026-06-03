import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .models import SyncResult, SyncStatus
from .state import SyncState

logger = logging.getLogger(__name__)


class GmailHubSpotSync:
    def __init__(
        self,
        gmail_credentials_file: str = "credentials.json",
        gmail_token_file: str = "token.json",
        hubspot_access_token: str = "",
        state_file: str = ".processed_messages.json",
        initial_lookback_days: int = 7,
        skip_emails: Optional[set] = None,
        skip_domains: Optional[set] = None,
        own_emails: Optional[set] = None,
    ):
        self._gmail = GmailClient(
            credentials_file=gmail_credentials_file,
            token_file=gmail_token_file,
        )
        self._hubspot = HubSpotClient(access_token=hubspot_access_token)
        self._state = SyncState(state_file=state_file)
        self._initial_lookback_days = initial_lookback_days
        self._skip_emails = skip_emails or set()
        self._skip_domains = skip_domains or set()
        self._own_emails = own_emails or set()

    def run_once(self) -> list[SyncResult]:
        """Execute one sync pass. Returns the list of results."""
        since = self._state.last_run
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(days=self._initial_lookback_days)
            logger.info("Prima esecuzione — recupero email degli ultimi %d giorni", self._initial_lookback_days)
        else:
            logger.info("Recupero email dal %s", since.isoformat())

        message_ids = self._gmail.list_inbox_message_ids(since=since)
        logger.info("Trovati %d messaggi da esaminare", len(message_ids))

        already_processed = self._state.processed_ids
        results: list[SyncResult] = []

        for msg_id in message_ids:
            if msg_id in already_processed:
                logger.debug("Messaggio %s già processato, salto", msg_id)
                continue

            contact = self._gmail.get_sender_contact(
                message_id=msg_id,
                skip_emails=self._skip_emails,
                skip_domains=self._skip_domains,
                own_emails=self._own_emails,
            )

            if contact is None:
                self._state.mark_processed(msg_id)
                continue

            logger.info("Processo mittente: %s", contact.email)
            result = self._hubspot.upsert_contact(contact)
            self._state.mark_processed(msg_id)
            results.append(result)

            _emoji = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭"}.get(result.status.value, "")
            logger.info("%s %s", _emoji, result)

        self._state.update_last_run()
        return results

    def print_summary(self, results: list[SyncResult]) -> None:
        if not results:
            print("Nessun contatto nuovo da processare in questo ciclo.")
            return

        created = [r for r in results if r.status == SyncStatus.CREATED]
        updated = [r for r in results if r.status == SyncStatus.UPDATED]
        ignored = [r for r in results if r.status == SyncStatus.IGNORED]

        print(f"\n{'='*60}")
        print(f"  RIEPILOGO SINCRONIZZAZIONE GMAIL → HUBSPOT")
        print(f"{'='*60}")
        print(f"  ✅ Creati:    {len(created)}")
        print(f"  🔄 Aggiornati: {len(updated)}")
        print(f"  ⏭  Ignorati:  {len(ignored)}")
        print(f"{'='*60}\n")

        all_active = created + updated
        if all_active:
            print(f"{'Stato':<12} {'Email':<45} {'ID HubSpot':<15} Dettaglio")
            print("-" * 90)
            for r in all_active:
                detail = r.detail or ""
                print(f"{r.status.value:<12} {r.email:<45} {r.hubspot_id or '':<15} {detail}")
        print()
