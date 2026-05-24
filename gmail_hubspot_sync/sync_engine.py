"""
sync_engine.py — Motore di sincronizzazione Gmail → HubSpot.

Gestisce il ciclo di polling:
1. Recupera nuovi messaggi da Gmail
2. Per ciascuno invoca HubSpot sync
3. Segna il messaggio come processato su Gmail
4. Produce un report di riepilogo
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from config import config
from gmail_client import GmailClient, SenderInfo
from hubspot_client import HubSpotClient, SyncResult, SyncStatus
from logger import get_logger

log = get_logger("sync", config.log_file)


@dataclass
class CycleReport:
    started_at: datetime = field(default_factory=datetime.now)
    results: list[SyncResult] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(1 for r in self.results if r.status == SyncStatus.CREATED)

    @property
    def updated(self) -> int:
        return sum(1 for r in self.results if r.status == SyncStatus.UPDATED)

    @property
    def ignored(self) -> int:
        return sum(1 for r in self.results if r.status == SyncStatus.IGNORED)

    @property
    def total(self) -> int:
        return len(self.results)

    def print_summary(self) -> None:
        elapsed = (datetime.now() - self.started_at).total_seconds()
        log.info("─" * 60)
        log.info(
            f"📊  Ciclo completato in {elapsed:.1f}s — "
            f"Tot={self.total}  🟢Creati={self.created}  "
            f"🔵Aggiornati={self.updated}  ⚪Ignorati={self.ignored}"
        )
        for result in self.results:
            log.info(str(result))
        log.info("─" * 60)


class SyncEngine:
    def __init__(self) -> None:
        self.gmail = GmailClient()
        self.hubspot = HubSpotClient()
        self._initialized = False

    def initialize(self) -> None:
        """Autentica i client. Da chiamare una sola volta all'avvio."""
        config.validate()
        self.gmail.authenticate()
        log.info("✅  HubSpot client inizializzato.")
        self._initialized = True

    def run_once(self) -> CycleReport:
        """
        Esegue un singolo ciclo di polling e ritorna il report.
        """
        if not self._initialized:
            raise RuntimeError("Chiama initialize() prima di run_once().")

        report = CycleReport()
        messages: list[SenderInfo] = self.gmail.fetch_new_messages()

        for sender in messages:
            try:
                result: SyncResult = self.hubspot.sync_sender(sender)
                report.results.append(result)
                # Segna sempre il messaggio su Gmail, indipendentemente dall'esito
                self.gmail.mark_as_processed(sender.message_id)
            except Exception as exc:
                log.error(f"Errore non gestito per {sender.email}: {exc}", exc_info=True)
                report.results.append(
                    SyncResult(
                        status=SyncStatus.IGNORED,
                        contact_email=sender.email,
                        contact_id=None,
                        detail=f"eccezione: {exc}",
                    )
                )

        return report

    def run_forever(self) -> None:
        """
        Loop infinito che esegue run_once() a intervalli regolari.
        Intervallo configurabile via POLL_INTERVAL_SECONDS.
        """
        log.info(
            f"🚀  Avvio sync continua — polling ogni {config.poll_interval_seconds}s"
        )
        while True:
            try:
                report = self.run_once()
                report.print_summary()
            except KeyboardInterrupt:
                log.info("Interruzione richiesta dall'utente. Uscita.")
                break
            except Exception as exc:
                log.error(f"Errore nel ciclo di sync: {exc}", exc_info=True)

            log.info(f"⏳  Prossimo ciclo tra {config.poll_interval_seconds}s...")
            time.sleep(config.poll_interval_seconds)
