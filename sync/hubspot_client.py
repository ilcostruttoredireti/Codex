import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.timeline.models import (
    TimelineEvent,
)

from .models import SenderInfo

log = logging.getLogger(__name__)

_SOURCE_LABEL = "Gmail"
_TAG_LABEL = "Inbound Gmail"

# HubSpot standard leadsource value closest to Gmail
_LEAD_SOURCE = "EMAIL_MARKETING"


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        self._client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the raw HubSpot contact dict or None."""
        f = Filter(property_name="email", operator="EQ", value=email)
        fg = FilterGroup(filters=[f])
        req = PublicObjectSearchRequest(
            filter_groups=[fg],
            properties=["email", "firstname", "lastname", "company", "leadsource", "hs_tag_ids"],
            limit=1,
        )
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=req
            )
            if resp.total > 0:
                c = resp.results[0]
                return {"id": c.id, "properties": c.properties}
        except ApiException as exc:
            log.error("HubSpot search error: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, sender: SenderInfo) -> Optional[str]:
        """Create a new contact. Returns the HubSpot contact ID or None."""
        props = {
            "email": sender.email,
            "firstname": sender.first_name,
            "lastname": sender.last_name,
            "leadsource": _LEAD_SOURCE,
            "hs_analytics_source": _SOURCE_LABEL,
        }
        if sender.company:
            props["company"] = sender.company

        try:
            obj = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            log.debug("Created HubSpot contact %s for %s", obj.id, sender.email)
            return obj.id
        except ApiException as exc:
            log.error("HubSpot create error for %s: %s", sender.email, exc)
            return None

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact_if_needed(
        self, contact_id: str, sender: SenderInfo
    ) -> bool:
        """Patch only the missing fields. Returns True if any update was made."""
        try:
            existing = self._client.crm.contacts.basic_api.get_by_id(
                contact_id,
                properties=["firstname", "lastname", "company", "leadsource"],
            )
        except ApiException as exc:
            log.error("HubSpot get contact %s error: %s", contact_id, exc)
            return False

        p = existing.properties or {}
        updates: dict[str, str] = {}

        if not p.get("firstname") and sender.first_name:
            updates["firstname"] = sender.first_name
        if not p.get("lastname") and sender.last_name:
            updates["lastname"] = sender.last_name
        if not p.get("company") and sender.company:
            updates["company"] = sender.company
        if not p.get("leadsource"):
            updates["leadsource"] = _LEAD_SOURCE

        if not updates:
            return False

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            log.debug("Updated HubSpot contact %s: %s", contact_id, list(updates))
            return True
        except ApiException as exc:
            log.error("HubSpot update contact %s error: %s", contact_id, exc)
            return False

    # ------------------------------------------------------------------
    # Timeline activity (optional)
    # ------------------------------------------------------------------

    def log_email_received(
        self,
        contact_id: str,
        sender_email: str,
        event_type_id: Optional[str] = None,
    ) -> None:
        """
        Record an 'email received' timeline event on the contact.
        Requires a custom Timeline Event Type set up in HubSpot.
        Set HUBSPOT_TIMELINE_EVENT_TYPE_ID in your env to enable this.
        """
        if not event_type_id:
            return
        try:
            event = TimelineEvent(
                event_type_id=event_type_id,
                object_id=contact_id,
                tokens={"senderEmail": sender_email},
            )
            self._client.crm.timeline.events_api.create(timeline_event=event)
        except Exception as exc:
            log.warning("Could not log timeline event for %s: %s", contact_id, exc)
