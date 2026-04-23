"""HubSpot client: search, create, and update contacts; log timeline activities."""

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicUpsertObject
from hubspot.crm.timeline import (
    TimelineEvent,
    ApiException as TimelineApiException,
)

logger = logging.getLogger(__name__)

# HubSpot timeline event type IDs must be pre-created via the API.
# Set this env var / config value if you have a custom event type.
TIMELINE_EVENT_TYPE_ID: Optional[str] = None


class SyncStatus(Enum):
    CREATED = auto()
    UPDATED = auto()
    IGNORED = auto()


@dataclass
class SyncResult:
    status: SyncStatus
    contact_email: str
    contact_id: Optional[str]
    detail: str = ""


class HubSpotClient:
    def __init__(self, api_key: str, timeline_event_type_id: Optional[str] = None):
        self._client = hubspot.Client.create(access_token=api_key)
        self._timeline_event_type_id = timeline_event_type_id or TIMELINE_EVENT_TYPE_ID
        logger.info("HubSpot client initialised.")

    # ------------------------------------------------------------------
    # Contact lookup
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing HubSpot contact dict or None."""
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email,
                        )
                    ]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            result = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if result.total > 0:
                return result.results[0].to_dict()
        except ApiException as exc:
            logger.error("HubSpot search failed for %s: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Create / update
    # ------------------------------------------------------------------

    def _build_properties(
        self,
        email: str,
        first_name: Optional[str],
        last_name: Optional[str],
        company: Optional[str],
        source: str = "Gmail",
    ) -> dict[str, str]:
        props: dict[str, str] = {"email": email, "hs_lead_status": "NEW"}
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company
        # Custom source property (must exist in your HubSpot portal as a text field)
        props["lead_source"] = source
        return props

    def create_contact(
        self,
        email: str,
        first_name: Optional[str],
        last_name: Optional[str],
        company: Optional[str],
    ) -> Optional[str]:
        """Create a new contact. Returns the HubSpot contact ID or None on error."""
        props = self._build_properties(email, first_name, last_name, company)
        try:
            contact = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            logger.info("Created contact %s (id=%s)", email, contact.id)
            return contact.id
        except ApiException as exc:
            logger.error("Failed to create contact %s: %s", email, exc)
            return None

    def update_contact(
        self,
        contact_id: str,
        existing: dict,
        email: str,
        first_name: Optional[str],
        last_name: Optional[str],
        company: Optional[str],
    ) -> bool:
        """
        Patch only fields that are currently empty in HubSpot.
        Returns True if any update was applied.
        """
        existing_props = existing.get("properties", {})
        updates: dict[str, str] = {}

        def _missing(key: str) -> bool:
            v = existing_props.get(key)
            return not v or str(v).strip() == ""

        if first_name and _missing("firstname"):
            updates["firstname"] = first_name
        if last_name and _missing("lastname"):
            updates["lastname"] = last_name
        if company and _missing("company"):
            updates["company"] = company
        if _missing("lead_source"):
            updates["lead_source"] = "Gmail"

        if not updates:
            return False

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
                    properties=updates
                ),
            )
            logger.info("Updated contact %s (id=%s) fields: %s", email, contact_id, list(updates))
            return True
        except ApiException as exc:
            logger.error("Failed to update contact %s: %s", email, exc)
            return False

    # ------------------------------------------------------------------
    # Timeline activity
    # ------------------------------------------------------------------

    def log_email_received(
        self,
        contact_id: str,
        subject: str,
        received_at: Optional[str],
    ) -> None:
        """Add a timeline event for the received email (best-effort)."""
        if not self._timeline_event_type_id:
            logger.debug("No timeline event type configured; skipping activity log.")
            return
        try:
            event = TimelineEvent(
                event_template_id=self._timeline_event_type_id,
                object_id=contact_id,
                tokens={"subject": subject},
                timestamp=received_at,
            )
            self._client.crm.timeline.events_api.create(timeline_event=event)
            logger.debug("Timeline event logged for contact %s.", contact_id)
        except Exception as exc:
            logger.debug("Timeline event skipped: %s", exc)

    # ------------------------------------------------------------------
    # High-level sync entry point
    # ------------------------------------------------------------------

    def sync_sender(
        self,
        email: str,
        first_name: Optional[str],
        last_name: Optional[str],
        company: Optional[str],
        subject: str = "",
        received_at: Optional[str] = None,
    ) -> SyncResult:
        """
        Ensure the sender exists in HubSpot.
        Returns a SyncResult with status CREATED / UPDATED / IGNORED.
        """
        existing = self.find_contact_by_email(email)

        if existing is None:
            contact_id = self.create_contact(email, first_name, last_name, company)
            if contact_id:
                self.log_email_received(contact_id, subject, received_at)
                return SyncResult(
                    status=SyncStatus.CREATED,
                    contact_email=email,
                    contact_id=contact_id,
                )
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=email,
                contact_id=None,
                detail="Creation failed — see logs.",
            )

        contact_id = str(existing["id"])
        updated = self.update_contact(
            contact_id, existing, email, first_name, last_name, company
        )
        self.log_email_received(contact_id, subject, received_at)

        if updated:
            return SyncResult(
                status=SyncStatus.UPDATED,
                contact_email=email,
                contact_id=contact_id,
            )
        return SyncResult(
            status=SyncStatus.IGNORED,
            contact_email=email,
            contact_id=contact_id,
            detail="Contact already complete — no fields updated.",
        )
