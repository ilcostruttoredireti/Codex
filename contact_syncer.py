"""Core sync logic: Gmail sender → HubSpot contact (create / update / skip)."""

import logging
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

# Personal / free email providers — domain is not a useful company name.
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "icloud.com", "me.com", "live.com",
    "live.it", "msn.com", "aol.com", "protonmail.com", "proton.me",
    "tutanota.com", "libero.it", "virgilio.it", "tiscali.it",
    "alice.it", "tin.it", "email.it", "fastwebnet.it",
}

# Email prefixes that indicate automated / transactional senders to skip.
SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
    "notifications", "notification", "newsletter", "newsletters",
    "unsubscribe", "automated", "automailer", "info", "support",
    "helpdesk", "admin", "billing", "invoices",
)


class ContactSyncer:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        create_timeline: bool = True,
        add_inbound_tag: bool = True,
    ):
        self.gmail = gmail
        self.hubspot = hubspot
        self.create_timeline = create_timeline
        self.add_inbound_tag = add_inbound_tag

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def process_message(self, message: dict) -> dict:
        """
        Process one Gmail message dict and sync the sender to HubSpot.

        Returns a result dict with keys:
            status      CREATO | AGGIORNATO | IGNORATO
            email       sender email address
            hubspot_id  HubSpot contact ID (or None)
            reason      why the message was ignored (if applicable)
        """
        sender = self.gmail.parse_sender(message["from"])
        email_addr = sender["email"]

        # --- guard: invalid address
        if not email_addr or "@" not in email_addr:
            return self._skip(email_addr, "Indirizzo email non valido")

        # --- guard: automated sender
        local_part = email_addr.split("@")[0]
        if any(local_part.startswith(p) for p in SKIP_PREFIXES):
            return self._skip(email_addr, "Mittente automatico/di sistema")

        # --- check HubSpot
        existing = self.hubspot.find_contact_by_email(email_addr)

        if existing:
            contact_id = existing["id"]
            updates = self._missing_fields(sender, existing["properties"])

            if self.add_inbound_tag:
                updates = self._add_tag_if_needed(updates, existing["properties"])

            if updates:
                self.hubspot.update_contact(contact_id, updates)
                status = "AGGIORNATO"
            else:
                return self._skip(email_addr, "Nessun campo da aggiornare", contact_id)
        else:
            props = self._build_create_props(sender)
            result = self.hubspot.create_contact(props)
            contact_id = result["id"]
            status = "CREATO"

        # --- optional timeline activity
        if self.create_timeline:
            self.hubspot.create_email_activity(
                contact_id,
                subject=message.get("subject", "(no subject)"),
                received_at=message.get("date", ""),
            )

        return {"status": status, "email": email_addr, "hubspot_id": contact_id}

    # ------------------------------------------------------------------
    # Property builders
    # ------------------------------------------------------------------

    def _build_create_props(self, sender: dict) -> dict:
        props: dict = {
            "email": sender["email"],
            # "OTHER" is the valid HubSpot enum value for leadsource.
            # The note field carries the human-readable "Gmail" label.
            "leadsource": "OTHER",
            "hs_analytics_source_data_1": "Gmail",
        }
        if sender["first_name"]:
            props["firstname"] = sender["first_name"]
        if sender["last_name"]:
            props["lastname"] = sender["last_name"]

        company = self._company_from_domain(sender["domain"])
        if company:
            props["company"] = company

        if self.add_inbound_tag:
            props["hs_lead_status"] = "NEW"
            # Store the "Inbound Gmail" label in the notes field so it's visible.
            props["notes_last_updated"] = "Inbound Gmail"

        return props

    def _missing_fields(self, sender: dict, existing: dict) -> dict:
        """Return only the properties that are currently blank on the contact."""
        updates: dict = {}

        if not existing.get("firstname") and sender["first_name"]:
            updates["firstname"] = sender["first_name"]
        if not existing.get("lastname") and sender["last_name"]:
            updates["lastname"] = sender["last_name"]
        if not existing.get("company"):
            company = self._company_from_domain(sender["domain"])
            if company:
                updates["company"] = company
        if not existing.get("leadsource"):
            updates["leadsource"] = "OTHER"
            updates["hs_analytics_source_data_1"] = "Gmail"

        return updates

    def _add_tag_if_needed(self, updates: dict, existing: dict) -> dict:
        """Append 'Inbound Gmail' marker only if not already present."""
        current_notes = existing.get("notes_last_updated", "") or ""
        if "Inbound Gmail" not in current_notes:
            updates["notes_last_updated"] = "Inbound Gmail"
        return updates

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _company_from_domain(domain: str) -> str | None:
        if not domain or domain in GENERIC_DOMAINS:
            return None
        # Strip TLD and humanise: "acme-corp.com" → "Acme Corp"
        name = domain.rsplit(".", 1)[0].replace("-", " ").replace("_", " ").title()
        return name or None

    @staticmethod
    def _skip(email: str, reason: str, contact_id: str | None = None) -> dict:
        return {
            "status": "IGNORATO",
            "email": email,
            "hubspot_id": contact_id,
            "reason": reason,
        }
