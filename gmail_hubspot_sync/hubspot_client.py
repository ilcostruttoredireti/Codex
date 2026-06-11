from dataclasses import dataclass
from datetime import datetime, timezone

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.engagements.notes import (
    SimplePublicObjectInputForCreate as NoteCreate,
)

import config


@dataclass
class ContactResult:
    status: str          # "created" | "updated" | "ignored"
    email: str
    contact_id: str | None
    reason: str = ""


def _client() -> hubspot.Client:
    return hubspot.Client.create(access_token=config.HUBSPOT_ACCESS_TOKEN)


def _search_by_email(client: hubspot.Client, email: str) -> dict | None:
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[
                    Filter(property_name="email", operator="EQ", value=email)
                ]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "lifecyclestage"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(req)
    except ApiException:
        return None
    return resp.results[0].to_dict() if resp.results else None


def _extract_company_from_domain(domain: str) -> str | None:
    """Best-effort: turn 'viagrandestudios.com' → 'Viagrande Studios'."""
    freemail = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                "icloud.com", "libero.it", "alice.it", "tiscali.it"}
    if domain in freemail:
        return None
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def _extract_names(display_name: str | None) -> tuple[str | None, str | None]:
    """Split 'First Last' into (firstname, lastname)."""
    if not display_name:
        return None, None
    parts = display_name.strip().split(maxsplit=1)
    firstname = parts[0] if parts else None
    lastname = parts[1] if len(parts) > 1 else None
    return firstname, lastname


def _add_note(client: hubspot.Client, contact_id: str, text: str) -> None:
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    note_input = NoteCreate(
        properties={
            "hs_note_body": text,
            "hs_timestamp": str(ts),
        }
    )
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note_input
        )
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception:
        pass  # note creation is best-effort


def upsert_contact(
    email: str,
    name: str | None,
    domain: str,
    subject: str,
) -> ContactResult:
    client = _client()
    existing = _search_by_email(client, email)

    note_body = (
        f"\U0001f4e7 Inbound Gmail\n"
        f"Data: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Oggetto: {subject}\n"
        f"Tag: Inbound Gmail"
    )

    if existing:
        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates: dict[str, str] = {}

        # Fill in missing fields only
        if not props.get("company"):
            company = _extract_company_from_domain(domain)
            if company:
                updates["company"] = company

        if not props.get("lifecyclestage"):
            updates["lifecyclestage"] = "lead"

        if not props.get("firstname") and name:
            fn, ln = _extract_names(name)
            if fn:
                updates["firstname"] = fn
            if ln and not props.get("lastname"):
                updates["lastname"] = ln

        if updates:
            try:
                client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input={"properties": updates},
                )
            except ApiException as e:
                return ContactResult("ignored", email, contact_id,
                                     reason=f"update failed: {e}")

        _add_note(client, contact_id, note_body)
        return ContactResult("updated", email, contact_id)

    # Create new contact
    firstname, lastname = _extract_names(name)
    company = _extract_company_from_domain(domain)

    props: dict[str, str] = {"email": email, "lifecyclestage": "lead"}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    try:
        contact = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
    except ApiException as e:
        return ContactResult("ignored", email, None, reason=f"create failed: {e}")

    _add_note(client, contact.id, note_body)
    return ContactResult("created", email, contact.id)
