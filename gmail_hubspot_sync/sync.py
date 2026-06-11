"""Core sync logic — orchestrates Gmail reading and HubSpot writing."""

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from .contact_parser import Contact, parse_sender
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient


class SyncStatus(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str]
    reason: Optional[str] = None


_MAX_STORED_IDS = 20_000


class GmailHubSpotSync:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state_file: str = "sync_state.json",
        dry_run: bool = False,
    ):
        self._gmail = gmail
        self._hubspot = hubspot
        self._state_path = Path(state_file)
        self._dry_run = dry_run
        self._processed: set[str] = self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> set:
        if self._state_path.exists():
            data = json.loads(self._state_path.read_text())
            return set(data.get("processed_ids", []))
        return set()

    def _save_state(self) -> None:
        ids = list(self._processed)[-_MAX_STORED_IDS:]
        self._state_path.write_text(
            json.dumps({"processed_ids": ids}, indent=2)
        )

    # ------------------------------------------------------------------
    # Per-message processing
    # ------------------------------------------------------------------

    def _merge_props(self, contact: Contact, existing: Optional[dict]) -> dict:
        """Return only the HubSpot properties that need to be written/updated."""
        existing_props = existing.get("properties", {}) if existing else {}

        def missing(key: str) -> bool:
            val = existing_props.get(key)
            return not val or str(val).strip() == ""

        props: dict = {}
        if missing("firstname") and contact.first_name:
            props["firstname"] = contact.first_name
        if missing("lastname") and contact.last_name:
            props["lastname"] = contact.last_name
        if missing("company") and contact.company:
            props["company"] = contact.company
        if missing("hs_lead_source"):
            props["hs_lead_source"] = "OTHER"
        return props

    def process_message(self, message_id: str) -> SyncResult:
        if message_id in self._processed:
            return SyncResult(SyncStatus.SKIPPED, "", None, "already processed")

        info = self._gmail.get_message_info(message_id)
        if not info or not info.get("from"):
            self._processed.add(message_id)
            return SyncResult(SyncStatus.SKIPPED, "", None, "no From header")

        contact = parse_sender(info["from"])
        if not contact:
            self._processed.add(message_id)
            return SyncResult(
                SyncStatus.SKIPPED,
                info.get("from", ""),
                None,
                "automated/invalid sender",
            )

        subject = info.get("subject", "—")
        date = info.get("date", "—")

        if self._dry_run:
            self._processed.add(message_id)
            return SyncResult(SyncStatus.CREATED, contact.email, "DRY-RUN")

        existing = self._hubspot.find_contact_by_email(contact.email)

        if existing:
            contact_id = str(existing["id"])
            update_props = self._merge_props(contact, existing)
            if update_props:
                self._hubspot.update_contact(contact_id, update_props)

            self._hubspot.add_note_to_contact(
                contact_id,
                f"📥 Email inbound ricevuta via Gmail\n"
                f"Da: {info['from']}\n"
                f"Oggetto: {subject}\n"
                f"Data: {date}\n"
                f"Fonte: Gmail | Tag: Inbound Gmail",
            )
            self._processed.add(message_id)
            return SyncResult(SyncStatus.UPDATED, contact.email, contact_id)

        # New contact
        create_props = self._merge_props(contact, None)
        create_props["email"] = contact.email
        create_props["hs_lead_source"] = "OTHER"

        result = self._hubspot.create_contact(create_props)
        if not result:
            return SyncResult(
                SyncStatus.SKIPPED, contact.email, None, "HubSpot create failed"
            )

        contact_id = str(result["id"])
        self._hubspot.add_note_to_contact(
            contact_id,
            f"✅ Contatto creato da email inbound Gmail\n"
            f"Da: {info['from']}\n"
            f"Oggetto: {subject}\n"
            f"Data: {date}\n"
            f"Fonte: Gmail | Tag: Inbound Gmail",
        )
        self._processed.add(message_id)
        return SyncResult(SyncStatus.CREATED, contact.email, contact_id)

    # ------------------------------------------------------------------
    # Public run methods
    # ------------------------------------------------------------------

    def run_once(self, batch_size: int = 100) -> list[SyncResult]:
        """Fetch inbox messages and process any not yet seen. Returns all results."""
        message_ids = self._gmail.list_inbox_message_ids(max_results=batch_size)
        results: list[SyncResult] = []

        for msg_id in message_ids:
            result = self.process_message(msg_id)
            results.append(result)
            _print_result(result)

        self._save_state()
        return results

    def run_continuous(self, interval: int = 60, batch_size: int = 100) -> None:
        print(
            f"[Sync] Avvio monitoraggio continuo (intervallo: {interval}s) "
            f"{'[DRY-RUN]' if self._dry_run else ''}"
        )
        while True:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n{'─'*50}\n[Sync] {ts}")
            try:
                results = self.run_once(batch_size)
                created = sum(1 for r in results if r.status == SyncStatus.CREATED)
                updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
                skipped = sum(1 for r in results if r.status == SyncStatus.SKIPPED)
                print(
                    f"[Sync] Riepilogo → Creati: {created} | "
                    f"Aggiornati: {updated} | Ignorati: {skipped}"
                )
            except Exception as exc:
                print(f"[Sync] Errore: {exc}")
            time.sleep(interval)


def _print_result(result: SyncResult) -> None:
    if result.status == SyncStatus.SKIPPED and not result.email:
        return  # silent skip for empty/already-processed messages
    parts = [f"  [{result.status.value:<10}] {result.email or '—'}"]
    if result.hubspot_id:
        parts.append(f"ID HubSpot: {result.hubspot_id}")
    if result.reason and result.status == SyncStatus.SKIPPED:
        parts.append(f"({result.reason})")
    print(" → ".join(parts))
