import logging
import time
from typing import Dict, Optional

from hubspot import HubSpot
from hubspot.crm.contacts import (
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
from hubspot.crm.contacts.exceptions import ApiException

logger = logging.getLogger(__name__)

CONTACT_PROPERTIES = ['email', 'firstname', 'lastname', 'company', 'leadsource']


class HubSpotClient:
    def __init__(self, access_token: str):
        self.client = HubSpot(access_token=access_token)

    def find_by_email(self, email: str) -> Optional[Dict]:
        """Return existing contact dict or None."""
        try:
            search_req = PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(filters=[
                        Filter(property_name='email', operator='EQ', value=email)
                    ])
                ],
                properties=CONTACT_PROPERTIES,
                limit=1,
            )
            result = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=search_req
            )
            if result.total > 0:
                c = result.results[0]
                return {'id': c.id, 'properties': c.properties or {}}
            return None
        except ApiException as exc:
            logger.error('HubSpot search error: %s', exc)
            return None

    def create_contact(self, properties: Dict[str, str]) -> Optional[str]:
        """Create contact and return its ID, or None on failure."""
        try:
            resp = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=properties
                )
            )
            return resp.id
        except ApiException as exc:
            logger.error('HubSpot create error: %s', exc)
            return None

    def update_contact(self, contact_id: str, properties: Dict[str, str]) -> bool:
        """Patch existing contact. Returns True on success."""
        try:
            self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=properties),
            )
            return True
        except ApiException as exc:
            logger.error('HubSpot update error: %s', exc)
            return False

    def add_email_note(self, contact_id: str, subject: str) -> None:
        """Attach a timeline note to the contact recording the inbound email."""
        try:
            from hubspot.crm.objects.notes import (
                SimplePublicObjectInputForCreate as NoteCreate,
            )
            from hubspot.crm.associations.v4.models import (
                AssociationSpec,
            )

            note_body = f'Email inbound ricevuta: {subject}' if subject else 'Email inbound ricevuta'
            note = self.client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteCreate(
                    properties={
                        'hs_note_body': note_body,
                        'hs_timestamp': str(int(time.time() * 1000)),
                    }
                )
            )
            # Associate note → contact (typeId 202 = Note to Contact)
            self.client.crm.associations.v4.basic_api.create(
                object_type='notes',
                object_id=note.id,
                to_object_type='contacts',
                to_object_id=contact_id,
                association_spec=[
                    AssociationSpec(
                        association_category='HUBSPOT_DEFINED',
                        association_type_id=202,
                    )
                ],
            )
        except Exception as exc:
            # Non-critical — log and continue
            logger.warning('HubSpot note error (non-fatal): %s', exc)
