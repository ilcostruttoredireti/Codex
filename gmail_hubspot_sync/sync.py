"""Logica principale di sincronizzazione Gmail → HubSpot."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Iterator

from .gmail_client import GmailClient, SenderInfo
from .hubspot_client import HubSpotClient, SyncResult, SyncStatus
from . import config

log = logging.getLogger(__name__)


class GmailHubSpotSync:
    """
    Orchestratore del ciclo di sincronizzazione.

    Flusso per ogni iterazione:
      1. Chiama GmailClient.get_new_senders() per ottenere i nuovi mittenti
      2. Per ciascuno, chiama HubSpotClient.sync_sender()
      3. Logga e accumula le statistiche
    """

    def __init__(self) -> None:
        self.gmail = GmailClient()
        self.hubspot = HubSpotClient()
        self._stats: dict[SyncStatus, int] = defaultdict(int)

    # ── Singola iterazione ───────────────────────────────────────────────────

    def run_once(self) -> list[SyncResult]:
        """
        Esegue una singola iterazione di sync.
        Restituisce la lista di SyncResult per tutte le email processate.
        """
        results: list[SyncResult] = []

        for sender in self.gmail.get_new_senders():
            log.info(
                "Elaboro email: from=%s <%s> | soggetto=%s",
                sender.display_name or "(no name)",
                sender.email,
                sender.subject or "(no subject)",
            )

            result = self.hubspot.sync_sender(
                email=sender.email,
                first_name=sender.first_name,
                last_name=sender.last_name,
                display_name=sender.display_name,
                domain=sender.domain,
                subject=sender.subject,
                message_id=sender.message_id,
            )

            self._stats[result.status] += 1
            results.append(result)
            self._print_result(result)

        return results

    # ── Loop continuo ────────────────────────────────────────────────────────

    def run_loop(self) -> None:
        """
        Esegue il ciclo di monitoraggio continuo.
        Si ferma solo con Ctrl+C (KeyboardInterrupt).
        """
        log.info(
            "Avvio monitoraggio Gmail → HubSpot "
            "(intervallo: %ds) — premi Ctrl+C per fermare",
            config.POLL_INTERVAL_SECONDS,
        )

        iteration = 0
        try:
            while True:
                iteration += 1
                log.debug("Iterazione #%d", iteration)

                try:
                    results = self.run_once()
                    if results:
                        self._print_summary(results)
                    else:
                        log.debug("Nessuna nuova email in questa iterazione.")
                except Exception as exc:  # pylint: disable=broad-except
                    # Non interrompiamo il loop per errori transitori
                    log.error("Errore durante l'iterazione #%d: %s", iteration, exc, exc_info=True)

                log.debug(
                    "Prossimo controllo tra %ds…", config.POLL_INTERVAL_SECONDS
                )
                time.sleep(config.POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            log.info("Monitoraggio fermato dall'utente.")
            self._print_global_stats()

    # ── Output ───────────────────────────────────────────────────────────────

    @staticmethod
    def _print_result(result: SyncResult) -> None:
        """Stampa una riga di output per ogni email processata."""
        icon = {
            SyncStatus.CREATED: "✅",
            SyncStatus.UPDATED: "🔄",
            SyncStatus.IGNORED: "⏭️ ",
            SyncStatus.ERROR:   "❌",
        }.get(result.status, "❓")
        print(f"{icon} {result}")

    def _print_summary(self, results: list[SyncResult]) -> None:
        """Stampa un riepilogo al termine di ogni batch."""
        counts = defaultdict(int)
        for r in results:
            counts[r.status] += 1
        summary = " | ".join(
            f"{s.value}: {n}" for s, n in counts.items()
        )
        log.info("Batch completato — %s", summary)

    def _print_global_stats(self) -> None:
        """Stampa le statistiche cumulative al termine del programma."""
        print("\n" + "─" * 50)
        print("📊 Statistiche totali:")
        for status, count in self._stats.items():
            print(f"   {status.value}: {count}")
        print("─" * 50)
