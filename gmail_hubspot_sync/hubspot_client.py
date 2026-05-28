import re
from dataclasses import dataclass
from typing import Optional

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# Known company names by domain fragment
_DOMAIN_COMPANY_MAP = {
    "cultura.gov.it": "Ministero della Cultura",
    "gov.it": "Pubblica Amministrazione",
    "comune.": None,  # handled dynamically
    "latestata.it": "La Testata",
    "liceodellearti.tn.it": "Liceo delle Arti TN",
    "unione.tn.it": "Unione Provincia Trento",
    "cooperativemarche.it": "Cooperative Marche",
    "artinmovimento.com": "Art in Movimento",
    "gabrielemichi.com": "giemmepress",
    "rec-media.it": "RECmedia",
}

_GMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it", "virgilio.it"}


def _company_from_domain(domain: str) -> Optional[str]:
    for fragment, company in _DOMAIN_COMPANY_MAP.items():
        if fragment in domain:
            return company
    if domain in _GMAIL_DOMAINS:
        return None
    # Capitalise the main domain as a best guess
    name = domain.split(".")[0].replace("-", " ").title()
    return name if len(name) > 2 else None


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "skipped"
    email: str
    contact_id: Optional[str]
    reason: Optional[str] = None


class HubSpotClient:
    def __init__(self, token: str):
        self._client = hubspot.Client.create(access_token=token)

    def find_contact(self, email: str) -> Optional[dict]:
        from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

        f = Filter(property_name="email", operator="EQ", value=email)
        fg = FilterGroup(filters=[f])
        req = PublicObjectSearchRequest(
            filter_groups=[fg],
            properties=["email", "firstname", "lastname", "company",
                        "hs_lead_source", "website", "hs_tag_ids"],
            limit=1,
        )
        resp = self._client.crm.contacts.search_api.do_search(public_object_search_request=req)
        results = resp.results
        return results[0].to_dict() if results else None

    def create_contact(self, email: str, first_name: Optional[str],
                       last_name: Optional[str], domain: str) -> SyncResult:
        company = _company_from_domain(domain)
        props = {
            "email": email,
            "hs_lead_source": CONTACT_SOURCE,
        }
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company

        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return SyncResult(status="created", email=email, contact_id=result.id)
        except ApiException as e:
            return SyncResult(status="skipped", email=email, contact_id=None,
                              reason=f"API error: {e.status} {e.reason}")

    def update_contact(self, contact_id: str, existing: dict,
                       first_name: Optional[str], last_name: Optional[str],
                       domain: str, email: str) -> SyncResult:
        props_to_set = existing.get("properties", {})
        updates: dict = {}

        if not props_to_set.get("firstname") and first_name:
            updates["firstname"] = first_name
        if not props_to_set.get("lastname") and last_name:
            updates["lastname"] = last_name
        if not props_to_set.get("company"):
            company = _company_from_domain(domain)
            if company:
                updates["company"] = company
        if not props_to_set.get("hs_lead_source"):
            updates["hs_lead_source"] = CONTACT_SOURCE

        if not updates:
            return SyncResult(status="skipped", email=email, contact_id=contact_id,
                              reason="no new fields to update")

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            return SyncResult(status="updated", email=email, contact_id=contact_id)
        except ApiException as e:
            return SyncResult(status="skipped", email=email, contact_id=contact_id,
                              reason=f"API error: {e.status} {e.reason}")

    def sync_sender(self, email: str, first_name: Optional[str],
                    last_name: Optional[str], domain: str) -> SyncResult:
        existing = self.find_contact(email)
        if existing:
            return self.update_contact(
                contact_id=str(existing["id"]),
                existing=existing,
                first_name=first_name,
                last_name=last_name,
                domain=domain,
                email=email,
            )
        return self.create_contact(email, first_name, last_name, domain)
