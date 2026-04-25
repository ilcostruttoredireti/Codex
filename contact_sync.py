import logging
from dataclasses import dataclass, field
from typing import Literal

from gmail_client import GmailClient
from hubspot_client import PERSONAL_DOMAINS, HubSpotClient

logger = logging.getLogger(__name__)

SyncStatus = Literal["created", "updated", "ignored", "error"]

# Addresses we never want to create contacts for
_SKIP_PREFIXES = ("no-reply", "noreply", "do-not-reply", "donotreply", "mailer-daemon", "postmaster")


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str = ""
    reason: str = ""
    updated_fields: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = [f"[{self.status.upper():8s}]", self.email]
        if self.contact_id:
            parts.append(f"ID:{self.contact_id}")
        if self.updated_fields:
            parts.append(f"fields={self.updated_fields}")
        if self.reason:
            parts.append(f"({self.reason})")
        return "  ".join(parts)


class ContactSyncer:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient):
        self._gmail = gmail
        self._hubspot = hubspot
        self._own_email = gmail.get_user_email().lower()
        logger.info("Monitoring inbox of: %s", self._own_email)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_messages(self, messages: list[dict]) -> list[SyncResult]:
        results: list[SyncResult] = []
        seen: set[str] = set()

        for msg in messages:
            info = GmailClient.extract_sender_info(msg)
            if not info:
                continue
            email = info["email"]
            if email in seen:
                continue
            seen.add(email)
            results.append(self._process_sender(info))

        return results

    # ------------------------------------------------------------------
    # Core sync logic
    # ------------------------------------------------------------------

    def _process_sender(self, info: dict) -> SyncResult:
        email = info["email"]

        skip_reason = self._should_skip(email)
        if skip_reason:
            return SyncResult("ignored", email, reason=skip_reason)

        try:
            existing = self._hubspot.find_contact_by_email(email)
            if existing:
                return self._update_if_needed(existing, info)
            return self._create_contact(info)
        except Exception as exc:
            logger.error("Error processing %s: %s", email, exc)
            return SyncResult("error", email, reason=str(exc))

    def _should_skip(self, email: str) -> str:
        if email == self._own_email:
            return "own address"
        local = email.split("@")[0].lower()
        if any(local.startswith(p) for p in _SKIP_PREFIXES):
            return "automated sender"
        return ""

    def _build_properties(self, info: dict) -> dict:
        props: dict[str, str] = {
            "email": info["email"],
            "lifecyclestage": "lead",
            "leadsource": "Gmail",
        }

        name = info.get("name", "")
        if name:
            parts = name.split(None, 1)
            props["firstname"] = parts[0]
            if len(parts) > 1:
                props["lastname"] = parts[1]

        domain = info.get("domain", "")
        if domain and domain not in PERSONAL_DOMAINS:
            # Turn "acme.com" → "Acme"
            props["company"] = domain.split(".")[0].capitalize()

        return props

    def _create_contact(self, info: dict) -> SyncResult:
        props = self._build_properties(info)
        contact = self._hubspot.create_contact(props)
        contact_id = contact["id"]

        note_body = (
            f"Fonte contatto: Inbound Gmail\n"
            f"Tag: Inbound Gmail\n"
            f"Prima email ricevuta: {info.get('subject', '(nessun oggetto)')}\n"
            f"Data: {info.get('date', '')}"
        )
        self._hubspot.add_note(contact_id, note_body)

        logger.info("Created  contact %s → HubSpot ID %s", info["email"], contact_id)
        return SyncResult("created", info["email"], contact_id)

    def _update_if_needed(self, existing: dict, info: dict) -> SyncResult:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        desired = self._build_properties(info)
        # Only patch fields that are currently empty in HubSpot
        patch = {
            k: v
            for k, v in desired.items()
            if k != "email" and not existing_props.get(k)
        }

        if not patch:
            return SyncResult(
                "ignored", info["email"], contact_id, reason="no new fields"
            )

        self._hubspot.update_contact(contact_id, patch)
        logger.info(
            "Updated  contact %s (ID %s): %s", info["email"], contact_id, list(patch)
        )
        return SyncResult(
            "updated", info["email"], contact_id, updated_fields=list(patch)
        )
