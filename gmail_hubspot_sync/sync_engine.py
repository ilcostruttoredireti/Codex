"""Orchestrates one full Gmail → HubSpot sync pass."""
import logging
from dataclasses import dataclass

from .gmail_client import iter_inbox_contacts
from .hubspot_client import upsert_contact

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    email: str
    status: str       # Creato | Aggiornato | Ignorato | Errore
    hubspot_id: str
    error: str = ""


def run_sync(days_back: int | None = None) -> list[SyncResult]:
    results = []
    for contact, message_id in iter_inbox_contacts(days_back):
        try:
            status, hs_id = upsert_contact(contact)
            results.append(SyncResult(email=contact.email, status=status, hubspot_id=hs_id))
            logger.info("[%s] %s → ID %s", status, contact.email, hs_id)
        except Exception as exc:
            logger.error("[Errore] %s: %s", contact.email, exc)
            results.append(SyncResult(email=contact.email, status="Errore", hubspot_id="", error=str(exc)))
    return results


def print_report(results: list[SyncResult]) -> None:
    print(f"\n{'─'*60}")
    print(f"{'STATO':<12} {'EMAIL':<40} {'HUBSPOT ID'}")
    print(f"{'─'*60}")
    for r in results:
        print(f"{r.status:<12} {r.email:<40} {r.hubspot_id or r.error}")
    print(f"{'─'*60}")
    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"Totale: {len(results)} | " + " | ".join(f"{k}: {v}" for k, v in counts.items() if v))
    print()
