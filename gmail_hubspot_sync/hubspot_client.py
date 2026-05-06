"""HubSpot API client for contact management."""

import logging
import time
from dataclasses import dataclass
from typing import Any

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicObject
from hubspot.crm.timeline import ApiException as TimelineApiException

logger = logging.getLogger(__name__)

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# HubSpot rate-limit: 10 req/s for free, 100 req/s for paid
_REQUEST_DELAY = 0.15  # seconds between API calls


@dataclass
class ContactResult:
    status: str        # "created" | "updated" | "ignored"
    email: str
    contact_id: str | None
    reason: str = ""


def _normalize_domain(domain: str) -> str:
    """Remove 'www.' prefix from domain."""
    return domain.removeprefix("www.")


class HubSpotClient:
    def __init__(self, access_token: str):
        self._client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> SimplePublicObject | None:
        """Return existing HubSpot contact matching `email`, or None."""
        body = PublicObjectSearchRequest(
            filter_groups=[
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status",
                         "lifecyclestage", "hubspot_owner_id"],
            limit=1,
        )
        try:
            time.sleep(_REQUEST_DELAY)
            result = self._client.crm.contacts.search_api.do_search(public_object_search_request=body)
            if result.total > 0:
                return result.results[0]
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(
        self,
        email: str,
        first_name: str | None,
        last_name: str | None,
        company: str | None,
    ) -> SimplePublicObject | None:
        props: dict[str, Any] = {
            "email": email,
            "hs_lead_source": CONTACT_SOURCE,
        }
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company

        body = SimplePublicObjectInputForCreate(properties=props)
        try:
            time.sleep(_REQUEST_DELAY)
            contact = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=body
            )
            logger.info("Created HubSpot contact %s (id=%s)", email, contact.id)
            return contact
        except ApiException as exc:
            logger.error("HubSpot create error for %s: %s", email, exc)
            return None

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(
        self,
        contact_id: str,
        existing: SimplePublicObject,
        first_name: str | None,
        last_name: str | None,
        company: str | None,
    ) -> bool:
        """Fill only blank fields; never overwrite existing data."""
        props: dict[str, Any] = {}
        ep = existing.properties or {}

        if first_name and not ep.get("firstname"):
            props["firstname"] = first_name
        if last_name and not ep.get("lastname"):
            props["lastname"] = last_name
        if company and not ep.get("company"):
            props["company"] = company

        if not props:
            logger.debug("No missing fields to update for contact %s", contact_id)
            return False

        try:
            time.sleep(_REQUEST_DELAY)
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input={"properties": props},
            )
            logger.info("Updated HubSpot contact %s with: %s", contact_id, list(props.keys()))
            return True
        except ApiException as exc:
            logger.error("HubSpot update error for %s: %s", contact_id, exc)
            return False

    # ------------------------------------------------------------------
    # Timeline activity (optional)
    # ------------------------------------------------------------------

    def log_email_activity(
        self,
        contact_id: str,
        subject: str,
        sender_email: str,
        received_at_ms: int,
    ) -> None:
        """
        Create a 'Note' engagement on the contact to record the inbound email.
        Uses the legacy engagements API which is available on all plans.
        """
        body = {
            "engagement": {
                "active": True,
                "type": "NOTE",
                "timestamp": received_at_ms,
            },
            "associations": {
                "contactIds": [int(contact_id)],
            },
            "metadata": {
                "body": (
                    f"<b>Inbound Gmail</b><br>"
                    f"From: {sender_email}<br>"
                    f"Subject: {subject or '(no subject)'}<br>"
                    f"Source: {CONTACT_SOURCE}"
                )
            },
        }
        try:
            time.sleep(_REQUEST_DELAY)
            self._client.api_client.call_api(
                "/engagements/v1/engagements",
                "POST",
                body=body,
                response_type=object,
                auth_settings=["hapikey", "oauth2"],
                _return_http_data_only=True,
            )
            logger.debug("Logged email activity for contact %s", contact_id)
        except Exception as exc:
            # Non-fatal: activity logging is optional
            logger.warning("Could not log activity for contact %s: %s", contact_id, exc)

    # ------------------------------------------------------------------
    # Main sync entry-point
    # ------------------------------------------------------------------

    def sync_contact(
        self,
        email: str,
        first_name: str | None,
        last_name: str | None,
        domain: str,
        subject: str,
        received_at_ms: int,
        log_activity: bool = True,
    ) -> ContactResult:
        company = _normalize_domain(domain) if domain else None

        existing = self.find_contact_by_email(email)

        if existing is None:
            contact = self.create_contact(email, first_name, last_name, company)
            if contact is None:
                return ContactResult(status="ignored", email=email, contact_id=None,
                                     reason="creation failed")
            if log_activity:
                self.log_email_activity(contact.id, subject, email, received_at_ms)
            return ContactResult(status="created", email=email, contact_id=contact.id)

        # Contact exists — update missing fields
        updated = self.update_contact(
            existing.id, existing, first_name, last_name, company
        )
        if log_activity:
            self.log_email_activity(existing.id, subject, email, received_at_ms)
        status = "updated" if updated else "ignored"
        return ContactResult(status=status, email=email, contact_id=existing.id,
                             reason="" if updated else "no missing fields")
