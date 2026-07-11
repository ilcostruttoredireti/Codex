import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput

from .config import Config
from .models import ContactInfo, SyncResult, SyncStatus

log = logging.getLogger(__name__)

# HubSpot property for contact source
SOURCE_PROPERTY = "hs_analytics_source_data_1"
INBOUND_TAG_PROPERTY = "hs_tag_ids"  # used if tag feature is enabled


class HubSpotClient:
    def __init__(self, cfg: Config):
        self._client = hubspot.Client.create(access_token=cfg.hubspot_access_token)

    def _find_by_email(self, email: str) -> Optional[dict]:
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [
                        {
                            "filters": [
                                {"propertyName": "email", "operator": "EQ", "value": email}
                            ]
                        }
                    ],
                    "properties": ["email", "firstname", "lastname", "company", "leadsource"],
                    "limit": 1,
                }
            )
            results = resp.results
            return results[0].to_dict() if results else None
        except ApiException as e:
            log.error("HubSpot search error for %s: %s", email, e)
            return None

    def _build_properties(self, contact: ContactInfo, existing: dict | None) -> dict:
        props: dict[str, str] = {
            "email": contact.email,
            "leadsource": "Gmail",
        }
        existing_props = (existing or {}).get("properties", {})

        if contact.first_name and not existing_props.get("firstname"):
            props["firstname"] = contact.first_name
        if contact.last_name and not existing_props.get("lastname"):
            props["lastname"] = contact.last_name
        if contact.company and not existing_props.get("company"):
            props["company"] = contact.company

        return props

    def upsert_contact(self, contact: ContactInfo) -> SyncResult:
        existing = self._find_by_email(contact.email)

        try:
            if existing:
                contact_id = existing["id"]
                props = self._build_properties(contact, existing)
                # Only update if there's something new to fill in
                update_props = {k: v for k, v in props.items() if k != "email"}
                if update_props:
                    self._client.crm.contacts.basic_api.update(
                        contact_id=contact_id,
                        simple_public_object_input=SimplePublicObjectInput(
                            properties=update_props
                        ),
                    )
                    return SyncResult(
                        status=SyncStatus.UPDATED,
                        email=contact.email,
                        hubspot_contact_id=str(contact_id),
                        details=f"Updated fields: {list(update_props.keys())}",
                    )
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    hubspot_contact_id=str(contact_id),
                    details="Contact exists with complete data",
                )
            else:
                props = self._build_properties(contact, None)
                new_contact = self._client.crm.contacts.basic_api.create(
                    simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                        properties=props
                    )
                )
                return SyncResult(
                    status=SyncStatus.CREATED,
                    email=contact.email,
                    hubspot_contact_id=str(new_contact.id),
                    details="New contact created from Gmail inbound",
                )
        except ApiException as e:
            log.error("HubSpot upsert error for %s: %s", contact.email, e)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=contact.email,
                error=str(e),
                details="API error during upsert",
            )
