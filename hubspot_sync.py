import logging
from dataclasses import dataclass
from enum import Enum

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str | None
    message: str = ""


class HubSpotSync:
    # Properties that identify a contact as coming from Gmail inbound
    SOURCE_LABEL = "Gmail"
    TAG_LABEL = "Inbound Gmail"

    def __init__(self, access_token: str):
        self._client = hubspot.Client.create(access_token=access_token)

    # ── Public interface ──────────────────────────────────────────────────────

    def sync_contact(self, sender: dict) -> SyncResult:
        """
        Given a sender dict from GmailMonitor.get_message_sender(), create or
        update the HubSpot contact. Returns a SyncResult.
        """
        email = sender["email"]
        existing = self._find_by_email(email)

        if existing:
            return self._update_contact(existing, sender)
        else:
            return self._create_contact(sender)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find_by_email(self, email: str) -> dict | None:
        """Search HubSpot contacts by email. Returns raw contact dict or None."""
        try:
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "email",
                                    "operator": "EQ",
                                    "value": email,
                                }
                            ]
                        }
                    ],
                    "properties": [
                        "email",
                        "firstname",
                        "lastname",
                        "company",
                        "hs_lead_status",
                        "lead_source_detail",
                        "hs_tag_ids",
                    ],
                    "limit": 1,
                }
            )
            results = response.results
            return results[0].to_dict() if results else None
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
            return None

    def _create_contact(self, sender: dict) -> SyncResult:
        email = sender["email"]
        props = self._build_properties(sender, existing_props={})

        try:
            contact = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props,
                    associations=[],
                )
            )
            contact_id = contact.id
            logger.info("Created HubSpot contact %s (%s)", email, contact_id)
            return SyncResult(
                status=SyncStatus.CREATED,
                email=email,
                contact_id=contact_id,
            )
        except ApiException as exc:
            # 409 = contact already exists (race condition); treat as update
            if exc.status == 409:
                logger.warning("Race condition on create for %s, retrying as update", email)
                existing = self._find_by_email(email)
                if existing:
                    return self._update_contact(existing, sender)
            logger.error("Failed to create contact %s: %s", email, exc)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=email,
                contact_id=None,
                message=str(exc),
            )

    def _update_contact(self, existing: dict, sender: dict) -> SyncResult:
        email = sender["email"]
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        # Only set fields that are currently blank in HubSpot
        props = self._build_properties(sender, existing_props=existing_props, update_mode=True)

        if not props:
            logger.debug("No missing fields for %s — skipping update", email)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=email,
                contact_id=contact_id,
                message="Nessun campo mancante da aggiornare",
            )

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            logger.info("Updated HubSpot contact %s (%s)", email, contact_id)
            return SyncResult(
                status=SyncStatus.UPDATED,
                email=email,
                contact_id=contact_id,
            )
        except ApiException as exc:
            logger.error("Failed to update contact %s: %s", email, exc)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=email,
                contact_id=contact_id,
                message=str(exc),
            )

    def _build_properties(
        self,
        sender: dict,
        existing_props: dict,
        update_mode: bool = False,
    ) -> dict:
        """
        Build the HubSpot property dict for create or update.
        In update_mode=True only include fields that are missing/blank.
        """
        candidates = {
            "email": sender["email"],
            "firstname": sender.get("first_name") or "",
            "lastname": sender.get("last_name") or "",
            "company": sender.get("company") or "",
            "lead_source_detail": self.SOURCE_LABEL,
        }

        if update_mode:
            props = {
                k: v
                for k, v in candidates.items()
                if v and not existing_props.get(k)
            }
            # Always try to set lead source if absent
            if not existing_props.get("lead_source_detail"):
                props["lead_source_detail"] = self.SOURCE_LABEL
        else:
            # Create: include all non-empty values
            props = {k: v for k, v in candidates.items() if v}

        return props
