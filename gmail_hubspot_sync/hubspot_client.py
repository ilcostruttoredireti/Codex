from dataclasses import dataclass
from typing import Optional

from . import config


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "skipped"
    email: str
    contact_id: Optional[str]
    reason: str = ""


def _client():
    import hubspot
    return hubspot.Client.create(access_token=config.HUBSPOT_ACCESS_TOKEN)


def _domain_to_company(domain: str) -> str:
    """'acme.com' → 'Acme' (best-effort heuristic)."""
    name = domain.split(".")[0]
    return name.capitalize()


def _build_properties(email: str, first_name: str, last_name: str, domain: str) -> dict:
    props: dict = {
        "email": email,
        "hs_lead_source": config.CONTACT_SOURCE,
    }
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if domain:
        props["company"] = _domain_to_company(domain)
    return props


def _find_contact(client, email: str) -> Optional[dict]:
    from hubspot.crm.contacts import Filter, FilterGroup, PublicObjectSearchRequest

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(filter_groups=[fg], properties=["email", "firstname", "lastname", "company"])
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.total > 0:
        return resp.results[0]
    return None


def _add_note(client, contact_id: str, subject: str, email: str) -> None:
    """Create an engagement note linking the inbound email."""
    try:
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput
        props = {
            "hs_note_body": f"Inbound email received from {email}. Subject: {subject}",
            "hs_timestamp": _now_ms(),
        }
        note_input = NoteInput(properties=props, associations=[])
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note_input
        )
        # Associate note → contact
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception:
        pass  # Notes are optional; don't fail the sync


def _now_ms() -> str:
    import time
    return str(int(time.time() * 1000))


def sync_contact(email: str, first_name: str, last_name: str, domain: str, subject: str) -> SyncResult:
    client = _client()

    existing = _find_contact(client, email)

    if existing is None:
        from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
        props = _build_properties(email, first_name, last_name, domain)
        props["hs_lead_source"] = config.CONTACT_SOURCE
        try:
            contact = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
            )
            _add_note(client, contact.id, subject, email)
            return SyncResult(status="created", email=email, contact_id=contact.id)
        except ApiException as exc:
            if exc.status == 409:
                # Duplicate created between search and create; retry as update
                return sync_contact(email, first_name, last_name, domain, subject)
            return SyncResult(status="skipped", email=email, contact_id=None, reason=str(exc))

    # Contact exists — fill in only missing fields
    from hubspot.crm.contacts.models import SimplePublicObjectInput
    contact_id = existing.id
    existing_props = existing.properties or {}
    updates: dict = {}

    if first_name and not existing_props.get("firstname"):
        updates["firstname"] = first_name
    if last_name and not existing_props.get("lastname"):
        updates["lastname"] = last_name
    if domain and not existing_props.get("company"):
        updates["company"] = _domain_to_company(domain)

    if updates:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        _add_note(client, contact_id, subject, email)
        return SyncResult(status="updated", email=email, contact_id=contact_id)

    _add_note(client, contact_id, subject, email)
    return SyncResult(status="skipped", email=email, contact_id=contact_id, reason="no new fields")
