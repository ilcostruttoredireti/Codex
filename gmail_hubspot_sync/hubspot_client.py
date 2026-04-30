import time
from typing import Optional

import requests

HUBSPOT_BASE = "https://api.hubapi.com"

# Note type identifier used when creating timeline activities
_NOTE_TYPE = "NOTE"

# HubSpot association type ID: note → contact
_ASSOC_NOTE_TO_CONTACT = "note_to_contact"


class HubSpotClient:
    def __init__(self, api_key: str, ignored_domains: set[str] | None = None):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.ignored_domains: set[str] = ignored_domains or set()

    # ------------------------------------------------------------------
    # Contact search / create / update
    # ------------------------------------------------------------------

    def search_by_email(self, email: str) -> Optional[dict]:
        """Return the first HubSpot contact matching *email*, or None."""
        payload = {
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
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
            json=payload,
            headers=self.headers,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
            json={"properties": properties},
            headers=self.headers,
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        resp = requests.patch(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
            headers=self.headers,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Timeline activity (note)
    # ------------------------------------------------------------------

    def add_inbound_note(self, contact_id: str, sender_email: str, subject: str = "") -> None:
        """Create a note on the contact timeline recording the inbound Gmail."""
        body_lines = [
            "Tag: Inbound Gmail",
            f"Fonte: Gmail",
            f"Mittente: {sender_email}",
        ]
        if subject:
            body_lines.append(f"Oggetto: {subject}")

        note_props = {
            "hs_note_body": "\n".join(body_lines),
            "hs_timestamp": str(int(time.time() * 1000)),
        }
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/notes",
            json={"properties": note_props},
            headers=self.headers,
        )
        if not resp.ok:
            return  # note creation is best-effort

        note_id = resp.json()["id"]
        requests.put(
            f"{HUBSPOT_BASE}/crm/v4/objects/notes/{note_id}/associations/contacts/{contact_id}",
            json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 1}],
            headers=self.headers,
        )

    # ------------------------------------------------------------------
    # High-level upsert
    # ------------------------------------------------------------------

    def upsert_contact(
        self,
        name: str,
        email: str,
        subject: str = "",
    ) -> tuple[str, str]:
        """
        Ensure *email* exists as a HubSpot contact.

        Returns (status, contact_id) where status is one of:
            "created"  – new contact was created
            "updated"  – existing contact had blank fields filled in
            "ignored"  – contact already complete; no changes needed
            "skipped"  – email is invalid or from an ignored domain
        """
        if not email or "@" not in email:
            return "skipped", ""

        domain = email.split("@")[1].lower()

        firstname, lastname = _split_name(name)
        company = _company_from_domain(domain, self.ignored_domains)

        existing = self.search_by_email(email)

        if existing:
            contact_id = existing["id"]
            props = existing.get("properties", {})
            updates: dict = {}

            if firstname and not props.get("firstname"):
                updates["firstname"] = firstname
            if lastname and not props.get("lastname"):
                updates["lastname"] = lastname
            if company and not props.get("company"):
                updates["company"] = company

            if updates:
                self.update_contact(contact_id, updates)
                self.add_inbound_note(contact_id, email, subject)
                return "updated", contact_id

            return "ignored", contact_id

        # Create new contact
        new_props: dict = {"email": email, "leadsource": "Gmail"}
        if firstname:
            new_props["firstname"] = firstname
        if lastname:
            new_props["lastname"] = lastname
        if company:
            new_props["company"] = company

        created = self.create_contact(new_props)
        contact_id = created["id"]
        self.add_inbound_note(contact_id, email, subject)
        return "created", contact_id


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _company_from_domain(domain: str, ignored_domains: set[str]) -> str:
    """Derive a company name from the email domain.

    Returns an empty string for personal email providers.
    """
    if domain in ignored_domains:
        return ""
    # Strip TLD(s) and capitalise: 'acmecorp.co.uk' → 'Acmecorp'
    name = domain.split(".")[0]
    return name.capitalize()
