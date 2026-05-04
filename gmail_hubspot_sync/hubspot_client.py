"""HubSpot API client — create, update, and search contacts."""

from __future__ import annotations

# hubspot SDK is imported lazily inside HubSpotClient.__init__ so that the
# pure helper functions (_build_properties, _company_from_domain) can be
# unit-tested without the SDK being importable.

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"


class HubSpotClient:
    def __init__(self, access_token: str):
        import hubspot as _hubspot

        self._client = _hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def upsert_contact(self, sender: dict) -> tuple[str, str]:
        """
        Create or update a contact from a Gmail sender record.

        Returns (status, contact_id) where status is one of:
            "created", "updated", "ignored"
        """
        email = sender["email"]
        existing = self._find_by_email(email)

        if existing:
            contact_id = existing.id
            updated = self._update_missing_fields(existing, sender)
            status = "updated" if updated else "ignored"
        else:
            contact_id = self._create_contact(sender)
            status = "created"

        return status, contact_id

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_by_email(self, email: str):
        from hubspot.crm.contacts import (
            ApiException,
            Filter,
            FilterGroup,
            PublicObjectSearchRequest,
        )

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
                return result.results[0]
        except ApiException as exc:
            raise RuntimeError(f"HubSpot search error: {exc}") from exc
        return None

    def _create_contact(self, sender: dict) -> str:
        from hubspot.crm.contacts import ApiException, SimplePublicObjectInputForCreate

        props = _build_properties(sender)
        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return result.id
        except ApiException as exc:
            raise RuntimeError(f"HubSpot create error: {exc}") from exc

    def _update_missing_fields(self, existing, sender: dict) -> bool:
        """Patch only blank/missing fields. Returns True if any update was sent."""
        from hubspot.crm.contacts import ApiException
        from hubspot.crm.contacts.models import SimplePublicObjectInput

        current = existing.properties or {}
        updates = {}

        def _fill(hs_key: str, value: str) -> None:
            if value and not current.get(hs_key):
                updates[hs_key] = value

        _fill("firstname", sender["first_name"])
        _fill("lastname", sender["last_name"])
        _fill("company", _company_from_domain(sender["domain"]))

        if not updates:
            return False

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=existing.id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
        except ApiException as exc:
            raise RuntimeError(f"HubSpot update error: {exc}") from exc
        return True


def _build_properties(sender: dict) -> dict:
    props = {
        "email": sender["email"],
        "hs_lead_status": "NEW",
        "leadsource": CONTACT_SOURCE,
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    company = _company_from_domain(sender["domain"])
    if company:
        props["company"] = company
    return props


def _company_from_domain(domain: str) -> str:
    """Return a human-readable company name derived from the email domain."""
    # Skip generic free-email providers
    generic = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "live.com", "msn.com", "aol.com",
    }
    if domain in generic:
        return ""
    # Strip common TLD and capitalise: "acme.io" → "Acme"
    name = domain.split(".")[0]
    return name.capitalize()
