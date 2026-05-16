import logging
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Optional

from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state import SyncState

logger = logging.getLogger(__name__)

# Common personal email domains — no company name can be inferred from these
_PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.fr",
        "hotmail.com", "hotmail.it", "outlook.com", "live.com", "live.it",
        "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com",
        "proton.me", "mail.com", "libero.it", "tiscali.it", "virgilio.it",
        "alice.it", "tin.it", "fastwebnet.it",
    }
)

# Automated sender patterns to skip
_AUTOMATED_PATTERNS = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|bounce|mailer-daemon|"
    r"postmaster|notifications?@|alerts?@|support@|newsletter@|"
    r"unsubscribe@|autoresponder@)",
    re.IGNORECASE,
)


class Status:
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class ProcessResult:
    status: str
    email: str = ""
    hubspot_id: str = ""
    reason: str = ""

    def __str__(self) -> str:
        icon = {"Creato": "✅", "Aggiornato": "♻️", "Ignorato": "⏭️"}.get(
            self.status, "❓"
        )
        parts = [f"{icon} [{self.status}]"]
        if self.email:
            parts.append(self.email)
        if self.hubspot_id:
            parts.append(f"HubSpot ID: {self.hubspot_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return "  ".join(parts)


def _extract_sender(headers: list[dict]) -> tuple[str, str]:
    """Returns (display_name, email_address) from message headers."""
    raw = next((h["value"] for h in headers if h["name"].lower() == "from"), "")
    name, addr = parseaddr(raw)
    return name.strip(), addr.strip().lower()


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def _company_from_email(email: str) -> str:
    """Derives a company name from the email domain, or '' for personal domains."""
    m = re.search(r"@(.+)$", email)
    if not m:
        return ""
    domain = m.group(1).lower()
    if domain in _PERSONAL_DOMAINS:
        return ""
    # "mail.acme.co.uk" → "acme", "acme.com" → "acme"
    parts = domain.split(".")
    root = parts[-2] if len(parts) >= 2 else parts[0]
    return root.capitalize()


def _build_create_props(name: str, email: str) -> dict[str, str]:
    firstname, lastname = _split_name(name)
    company = _company_from_email(email)
    props: dict[str, str] = {
        "email": email,
        "lifecyclestage": "lead",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def _build_update_props(
    name: str, email: str, existing_props: dict[str, Any]
) -> dict[str, str]:
    """Returns only the fields that are currently blank on the existing contact."""
    firstname, lastname = _split_name(name)
    company = _company_from_email(email)

    candidates = {
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
    }
    return {
        k: v
        for k, v in candidates.items()
        if v and not existing_props.get(k)
    }


class GmailHubSpotSync:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state: SyncState,
    ):
        self.gmail = gmail
        self.hubspot = hubspot
        self.state = state

    # ------------------------------------------------------------------
    # Single message processing
    # ------------------------------------------------------------------

    def process_message(self, message_id: str) -> ProcessResult:
        if self.state.is_processed(message_id):
            return ProcessResult(Status.IGNORED, reason="already_processed")

        msg = self.gmail.get_message(message_id)
        if not msg:
            return ProcessResult(Status.IGNORED, reason="fetch_failed")

        headers = msg.get("payload", {}).get("headers", [])
        name, email = _extract_sender(headers)

        if not email or "@" not in email:
            self.state.mark_processed(message_id)
            return ProcessResult(Status.IGNORED, reason="invalid_email")

        if _AUTOMATED_PATTERNS.search(email):
            self.state.mark_processed(message_id)
            return ProcessResult(Status.IGNORED, email=email, reason="automated_sender")

        result = self._sync_contact(name, email)
        self.state.mark_processed(message_id)
        return result

    def _sync_contact(self, name: str, email: str) -> ProcessResult:
        existing = self.hubspot.find_contact_by_email(email)

        if existing:
            update_props = _build_update_props(name, email, existing.properties or {})
            if not update_props:
                return ProcessResult(
                    Status.IGNORED,
                    email=email,
                    hubspot_id=existing.id,
                    reason="no_new_fields",
                )
            self.hubspot.update_contact(existing.id, update_props)
            return ProcessResult(Status.UPDATED, email=email, hubspot_id=existing.id)

        # New contact
        contact = self.hubspot.create_contact(_build_create_props(name, email))
        if not contact:
            return ProcessResult(Status.IGNORED, email=email, reason="create_failed")

        self.hubspot.add_activity_note(
            contact.id,
            (
                f"Email ricevuta via Gmail.\n"
                f"Mittente: {name or email}\n"
                f"Fonte contatto: Gmail\n"
                f"Tag: Inbound Gmail"
            ),
        )
        return ProcessResult(Status.CREATED, email=email, hubspot_id=contact.id)

    # ------------------------------------------------------------------
    # Polling cycle
    # ------------------------------------------------------------------

    def run_once(self, label: str = "INBOX") -> list[ProcessResult]:
        results: list[ProcessResult] = []

        if not self.state.history_id:
            results = self._initial_sync(label)
        else:
            results = self._incremental_sync(label)

        return results

    def _initial_sync(self, label: str) -> list[ProcessResult]:
        """First run: seed the historyId and process recent unread inbox emails."""
        profile = self.gmail.get_profile()
        self.state.update_history_id(profile["historyId"])
        logger.info(
            f"First run — history seed: {profile['historyId']}. "
            f"Processing unread inbox emails from the past 24 h."
        )
        messages = self.gmail.list_messages(
            query=f"in:{label.lower()} is:unread newer_than:1d"
        )
        return self._process_batch(messages)

    def _incremental_sync(self, label: str) -> list[ProcessResult]:
        """Subsequent runs: only process messages added since last historyId."""
        history = self.gmail.get_history(self.state.history_id, label)

        if history.get("expired"):
            # historyId too old — re-seed and skip this cycle
            profile = self.gmail.get_profile()
            self.state.update_history_id(profile["historyId"])
            logger.info("History expired, re-seeded historyId — will pick up new mail next cycle")
            return []

        new_history_id = history.get("historyId", self.state.history_id)
        if new_history_id != self.state.history_id:
            self.state.update_history_id(new_history_id)

        messages = []
        for record in history.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                label_ids = msg.get("labelIds", [])
                # Only genuine inbound inbox messages (skip sent/drafts)
                if "INBOX" in label_ids and "SENT" not in label_ids:
                    messages.append({"id": msg["id"]})

        return self._process_batch(messages)

    def _process_batch(self, message_refs: list[dict]) -> list[ProcessResult]:
        results = []
        for ref in message_refs:
            r = self.process_message(ref["id"])
            results.append(r)
            if r.email or r.hubspot_id:
                logger.info(str(r))
        return results
