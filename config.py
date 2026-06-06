"""Configurazione da variabili d'ambiente (.env)."""

import os
import logging
from dotenv import load_dotenv

load_dotenv()


def get_config() -> dict:
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    if not hubspot_token:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN mancante.\n"
            "Crea un Private App in HubSpot con scope:\n"
            "  crm.objects.contacts.read, crm.objects.contacts.write\n"
            "Poi imposta HUBSPOT_ACCESS_TOKEN nel file .env"
        )

    ignore_domains_raw = os.getenv("IGNORE_DOMAINS", "noreply.com,no-reply.com,mailer-daemon.org")
    ignore_emails_raw = os.getenv("IGNORE_EMAILS", "")

    return {
        "hubspot_access_token": hubspot_token,
        "poll_interval": int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        "state_file": os.getenv("STATE_FILE", ".sync_state.json"),
        "ignore_domains": {d.strip().lower() for d in ignore_domains_raw.split(",") if d.strip()},
        "ignore_emails": {e.strip().lower() for e in ignore_emails_raw.split(",") if e.strip()},
        "log_level": os.getenv("LOG_LEVEL", "INFO").upper(),
        "add_timeline_note": os.getenv("ADD_TIMELINE_NOTE", "true").lower() == "true",
    }


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
