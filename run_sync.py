"""
Punto di ingresso del sync Gmail → HubSpot.
Eseguibile manualmente o da un cron/scheduler.

Dipendenze MCP richieste:
  - Gmail MCP (search_threads, get_thread)
  - HubSpot MCP (search_crm_objects, manage_crm_objects)
"""

import json
import logging
import sys
from datetime import datetime, timezone

# In un contesto reale queste sarebbero chiamate ai MCP tool tramite SDK.
# Qui le funzioni sono stub documentati che mostrano il flusso di esecuzione.

log = logging.getLogger(__name__)

LOOKBACK_QUERY = "in:inbox -from:me -is:spam newer_than:1d"
MAX_THREADS = 50
OWN_EMAILS = {
    "cristian.mameli.editore@gmail.com",
    "pubblica.latestata@gmail.com",
    "redazione@latestata.it",
}


def run_sync(gmail_client, hubspot_client) -> dict:
    """
    Esegue un ciclo completo di sincronizzazione.

    Args:
        gmail_client:   client con metodi search_threads(query, page_size) e get_thread(id)
        hubspot_client: client con metodi search_contact(email) e upsert_contact(data, existing_id)

    Returns:
        dict con chiavi created, updated, skipped, errors
    """
    from gmail_hubspot_sync import (
        build_contact_from_message,
        sync_contact_to_hubspot,
        process_results,
        format_report,
    )

    results = {"created": [], "updated": [], "skipped": [], "errors": []}
    seen_emails: set[str] = set()

    threads = gmail_client.search_threads(query=LOOKBACK_QUERY, page_size=MAX_THREADS)
    log.info(f"Thread trovati: {len(threads)}")

    for thread in threads:
        thread_id = thread["id"]
        try:
            full = gmail_client.get_thread(thread_id)
            for msg in full.get("messages", []):
                contact = build_contact_from_message(msg, thread_id)
                if not contact:
                    continue
                if contact.email in seen_emails:
                    log.debug(f"Duplicato in questa esecuzione: {contact.email}")
                    continue
                seen_emails.add(contact.email)

                hs_hits = hubspot_client.search_contact(contact.email)
                contact = sync_contact_to_hubspot(contact, hs_hits)

                if contact.status == "CREATO":
                    hubspot_client.upsert_contact(
                        {
                            "email": contact.email,
                            "firstname": contact.firstname,
                            "lastname": contact.lastname,
                            "company": contact.company,
                        },
                        existing_id=None,
                    )
                    results["created"].append(contact)
                else:
                    updates = {}
                    props = hs_hits[0].get("properties", {}) if hs_hits else {}
                    if not props.get("company") and contact.company:
                        updates["company"] = contact.company
                    if not props.get("firstname") and contact.firstname:
                        updates["firstname"] = contact.firstname
                    if updates:
                        hubspot_client.upsert_contact(updates, existing_id=contact.hubspot_id)
                    results["updated"].append(contact)

        except Exception as exc:
            log.error(f"Errore sul thread {thread_id}: {exc}")
            results["errors"].append({"thread_id": thread_id, "error": str(exc)})

    all_contacts = results["created"] + results["updated"]
    report = format_report(process_results(all_contacts))
    log.info("\n" + report)
    return results


if __name__ == "__main__":
    print("Avvia il sync tramite Claude Code + MCP (Gmail + HubSpot).")
    print("Questo script è il punto di ingresso per l'esecuzione schedulata.")
