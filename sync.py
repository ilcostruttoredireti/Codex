import logging
from typing import Optional

import config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient, _build_props
from models import EmailContact, SyncResult, SyncStatus
from state import SyncState

logger = logging.getLogger(__name__)


class GmailHubSpotSync:
    def __init__(self) -> None:
        self.gmail = GmailClient(config.GMAIL_CREDENTIALS_FILE, config.GMAIL_TOKEN_FILE)
        self.hubspot = HubSpotClient(config.HUBSPOT_ACCESS_TOKEN)
        self.state = SyncState(config.STATE_FILE)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        self.gmail.authenticate()
        if not self.state.last_history_id:
            self.state.last_history_id = self.gmail.get_profile_history_id()
            self.state.save()
            logger.info(f"Stato inizializzato: historyId={self.state.last_history_id}")

    # ------------------------------------------------------------------
    # Run modes
    # ------------------------------------------------------------------

    def run_once(self) -> list[SyncResult]:
        messages, new_history_id = self.gmail.get_new_messages(self.state.last_history_id)

        results: list[SyncResult] = []
        for msg in messages:
            contact = self.gmail.get_message_sender(msg["id"])
            if contact is None:
                continue
            result = self._process_contact(contact)
            results.append(result)
            print(result)

        self.state.last_history_id = new_history_id
        self.state.processed_count += len(results)
        self.state.save()
        return results

    def run_continuous(self, interval: int) -> None:
        import time

        logger.info(f"Monitoraggio continuo avviato (intervallo: {interval}s)")
        while True:
            try:
                results = self.run_once()
                if results:
                    logger.info(f"Ciclo completato: {len(results)} email processate")
                else:
                    logger.debug("Nessuna nuova email")
            except Exception as exc:
                logger.error(f"Errore nel ciclo di sync: {exc}", exc_info=True)
            time.sleep(interval)

    # ------------------------------------------------------------------
    # Core contact processing
    # ------------------------------------------------------------------

    def _process_contact(self, contact: EmailContact) -> SyncResult:
        if _should_ignore(contact):
            logger.debug(f"Ignorato: {contact.email}")
            return SyncResult(status=SyncStatus.IGNORED, email=contact.email)

        contact.company = _infer_company(contact)

        existing = self.hubspot.find_contact_by_email(contact.email)

        if existing:
            contact_id = existing["id"]
            updates = _compute_updates(existing.get("properties", {}), contact)
            if updates:
                self.hubspot.update_contact(contact_id, updates)
                status = SyncStatus.UPDATED
            else:
                status = SyncStatus.IGNORED

            self.hubspot.create_note(contact_id, _build_note(contact))

            return SyncResult(
                status=status,
                email=contact.email,
                hubspot_contact_id=contact_id,
            )

        # New contact
        contact_id = self.hubspot.create_contact(contact)
        if not contact_id:
            return SyncResult(
                status=SyncStatus.ERROR,
                email=contact.email,
                error="Creazione contatto fallita",
            )

        self.hubspot.create_note(contact_id, _build_note(contact))
        return SyncResult(
            status=SyncStatus.CREATED,
            email=contact.email,
            hubspot_contact_id=contact_id,
        )


# ------------------------------------------------------------------
# Pure helpers (module-level for easy testing)
# ------------------------------------------------------------------

def _should_ignore(contact: EmailContact) -> bool:
    email = contact.email
    if not email or "@" not in email:
        return True
    if email in config.IGNORE_EMAILS:
        return True
    if contact.domain and contact.domain in config.IGNORE_DOMAINS:
        return True
    local = email.split("@")[0]
    if local in ("noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster",
                 "bounce", "bounces", "notifications", "newsletter"):
        return True
    return False


def _infer_company(contact: EmailContact) -> Optional[str]:
    domain = contact.domain
    if not domain or domain in config.FREE_EMAIL_DOMAINS:
        return None
    # Take the registrable part: last two labels (handles subdomains like mail.acme.com)
    parts = domain.split(".")
    registrable = parts[-2] if len(parts) >= 2 else parts[0]
    return registrable.replace("-", " ").title() or None


def _compute_updates(existing_props: dict, contact: EmailContact) -> dict:
    """Return only the fields that are missing in HubSpot and present in the new contact."""
    updates: dict = {}
    if not existing_props.get("firstname") and contact.first_name:
        updates["firstname"] = contact.first_name
    if not existing_props.get("lastname") and contact.last_name:
        updates["lastname"] = contact.last_name
    if not existing_props.get("company") and contact.company:
        updates["company"] = contact.company
    return updates


def _build_note(contact: EmailContact) -> str:
    lines = [
        f"Email ricevuta da: {contact.full_name or contact.email} <{contact.email}>",
    ]
    if contact.subject:
        lines.append(f"Oggetto: {contact.subject}")
    lines += [
        "",
        "Tag: Inbound Gmail",
        "Fonte: Gmail",
    ]
    return "\n".join(lines)
