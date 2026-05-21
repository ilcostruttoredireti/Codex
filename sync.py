"""
Logica di sincronizzazione: prende i mittenti estratti da Gmail
e li sincronizza in HubSpot, restituendo un report strutturato.
"""

import logging
from dataclasses import dataclass
from typing import List

from gmail_client import SenderInfo
from hubspot_client import SyncResult, SyncStatus, upsert_contact

logger = logging.getLogger(__name__)


@dataclass
class ProcessingReport:
    total: int
    created: int
    updated: int
    ignored: int
    errors: int
    results: List[SyncResult]


def process_senders(senders: List[SenderInfo], email_subject: str = "") -> ProcessingReport:
    """
    Sincronizza una lista di mittenti Gmail in HubSpot.
    Restituisce un report con i risultati per ogni contatto.
    """
    report = ProcessingReport(
        total=len(senders),
        created=0,
        updated=0,
        ignored=0,
        errors=0,
        results=[],
    )

    # Deduplica per email nella stessa batch (più email dallo stesso mittente)
    seen: set[str] = set()
    unique_senders = []
    for s in senders:
        if s.email not in seen:
            seen.add(s.email)
            unique_senders.append(s)

    for sender in unique_senders:
        try:
            result = upsert_contact(
                email=sender.email,
                first_name=sender.first_name,
                last_name=sender.last_name,
                company=sender.company,
                add_tag=True,
                create_note=True,
                note_subject=email_subject or f"Email da {sender.email}",
            )
        except Exception as e:
            logger.error("Errore sincronizzazione %s: %s", sender.email, e)
            result = SyncResult(
                status=SyncStatus.IGNORED,
                email=sender.email,
                contact_id=None,
                message=f"Errore: {e}",
            )
            report.errors += 1

        report.results.append(result)

        if result.status == SyncStatus.CREATED:
            report.created += 1
        elif result.status == SyncStatus.UPDATED:
            report.updated += 1
        else:
            report.ignored += 1

    return report
