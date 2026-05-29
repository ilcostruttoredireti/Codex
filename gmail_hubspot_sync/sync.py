#!/usr/bin/env python3
"""Gmail → HubSpot contact sync.

Polls the Gmail inbox for new messages and upserts sender contacts into HubSpot.

Usage:
    python -m gmail_hubspot_sync.sync           # run once then exit
    python -m gmail_hubspot_sync.sync --loop    # poll continuously
    python -m gmail_hubspot_sync.sync --dry-run # preview without writing
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .config import Config
from .contact_parser import ContactInfo, parse_sender
from .gmail_client import EmailMessage, GmailClient
from .hubspot_client import HubSpotClient
from .state_manager import StateManager


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    SKIPPED = "Saltato"  # automated / no-reply sender


@dataclass
class SyncResult:
    status: SyncStatus
    sender_email: str
    contact_id: Optional[str]
    reason: str = ""

    def __str__(self) -> str:
        cid = self.contact_id or "-"
        reason = f" ({self.reason})" if self.reason else ""
        return f"[{self.status.value:10}] {self.sender_email:<40} ID={cid}{reason}"


class GmailHubSpotSync:
    INBOUND_LABEL = "Inbound Gmail"

    def __init__(self, config: Config, dry_run: bool = False) -> None:
        self._cfg = config
        self._dry_run = dry_run
        self._state = StateManager(config.state_file)
        self._gmail = GmailClient(config.gmail_credentials_file, config.gmail_token_file)
        self._hubspot = HubSpotClient(config.hubspot_access_token)
        self._log = logging.getLogger(self.__class__.__name__)

    def run_once(self) -> list[SyncResult]:
        """Process new inbox messages and return results."""
        since = self._state.last_checked_at
        self._log.info("Fetching messages since %s", since or "the beginning")

        messages = self._gmail.list_inbox_messages(after=since)
        self._log.info("Found %d message(s) to process", len(messages))

        results: list[SyncResult] = []
        for msg in messages:
            if self._state.is_processed(msg.message_id):
                continue
            result = self._process_message(msg)
            results.append(result)
            self._log.info(str(result))
            self._state.mark_processed(msg.message_id)

        self._state.mark_checked_now()
        return results

    def _process_message(self, msg: EmailMessage) -> SyncResult:
        contact = parse_sender(msg.from_header)

        if contact.should_skip or not contact.email:
            return SyncResult(
                status=SyncStatus.SKIPPED,
                sender_email=contact.email or msg.from_header,
                contact_id=None,
                reason=contact.skip_reason or "no_email",
            )

        if self._dry_run:
            return SyncResult(
                status=SyncStatus.IGNORED,
                sender_email=contact.email,
                contact_id=None,
                reason="dry_run",
            )

        return self._upsert_contact(contact, msg)

    def _upsert_contact(self, contact: ContactInfo, msg: EmailMessage) -> SyncResult:
        existing = self._hubspot.find_contact_by_email(contact.email)

        if existing:
            updated = self._hubspot.update_contact(
                existing.contact_id, contact.firstname, contact.lastname,
                contact.company, existing
            )
            self._hubspot.add_inbound_note(
                existing.contact_id, contact.email, msg.subject,
                msg.date.isoformat()
            )
            if self._cfg.label_processed:
                self._gmail.add_label(msg.message_id, self.INBOUND_LABEL)
            status = SyncStatus.UPDATED if updated else SyncStatus.IGNORED
            return SyncResult(
                status=status,
                sender_email=contact.email,
                contact_id=existing.contact_id,
                reason="" if updated else "no_new_fields",
            )

        contact_id = self._hubspot.create_contact(
            contact.email, contact.firstname, contact.lastname, contact.company
        )
        self._hubspot.add_inbound_note(
            contact_id, contact.email, msg.subject, msg.date.isoformat()
        )
        if self._cfg.label_processed:
            self._gmail.add_label(msg.message_id, self.INBOUND_LABEL)
        return SyncResult(
            status=SyncStatus.CREATED,
            sender_email=contact.email,
            contact_id=contact_id,
        )


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=getattr(logging, level.upper(), logging.INFO),
    )


def _print_summary(results: list[SyncResult]) -> None:
    counts = {s: 0 for s in SyncStatus}
    for r in results:
        counts[r.status] += 1
    print(
        f"\nRiepilogo: "
        f"Creati={counts[SyncStatus.CREATED]}  "
        f"Aggiornati={counts[SyncStatus.UPDATED]}  "
        f"Ignorati={counts[SyncStatus.IGNORED]}  "
        f"Saltati={counts[SyncStatus.SKIPPED]}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--interval", type=int, default=None,
                        help="Override poll interval (seconds)")
    args = parser.parse_args()

    cfg = Config.from_env()
    _setup_logging(cfg.log_level)
    log = logging.getLogger("sync")

    if not args.dry_run:
        cfg.validate()

    if args.interval:
        cfg.poll_interval_seconds = args.interval

    syncer = GmailHubSpotSync(cfg, dry_run=args.dry_run)

    if args.dry_run:
        log.info("DRY RUN — nessuna modifica verrà scritta")

    stop = False

    def _handle_signal(sig, frame):  # noqa: ANN001
        nonlocal stop
        log.info("Interruzione ricevuta, uscita al prossimo ciclo...")
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not stop:
        try:
            results = syncer.run_once()
            for r in results:
                print(str(r))
            _print_summary(results)
        except Exception as e:
            log.error("Errore durante il sync: %s", e, exc_info=True)

        if not args.loop or stop:
            break

        log.info("Prossimo controllo tra %d secondi...", cfg.poll_interval_seconds)
        # Interruptible sleep
        for _ in range(cfg.poll_interval_seconds):
            if stop:
                break
            time.sleep(1)

    log.info("Sync terminato.")


if __name__ == "__main__":
    main()
