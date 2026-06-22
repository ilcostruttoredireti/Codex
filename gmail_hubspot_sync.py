"""
Gmail → HubSpot Contact Sync
Legge le email in arrivo da Gmail ed esegue upsert dei mittenti su HubSpot.

Utilizzo (come sessione Claude schedulata con MCP tools):
  python gmail_hubspot_sync.py --hours 24

Dipendenze: google-api-python-client, hubspot-api-client
"""

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

SKIP_DOMAINS = {"gmail.com", "googlemail.com", "facebookmail.com", "mailer-daemon.org"}
SKIP_PREFIXES = {"noreply@", "no-reply@", "notifications@", "pageupdates@", "donotreply@"}


@dataclass
class SenderInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""

    @staticmethod
    def from_raw(raw_sender: str) -> Optional["SenderInfo"]:
        """Parse a raw From header value into a SenderInfo."""
        name, addr = parseaddr(raw_sender)
        if not addr or "@" not in addr:
            return None
        addr = addr.lower().strip()

        # Skip own-account and automated senders
        if any(addr.startswith(p) for p in SKIP_PREFIXES):
            return None

        domain = addr.split("@")[-1]
        if domain in SKIP_DOMAINS:
            # Allow explicit personal names on personal-mail domains
            if not name:
                return None

        firstname, lastname = _split_name(name)
        company = _company_from_domain(domain) if domain not in SKIP_DOMAINS else ""

        return SenderInfo(
            email=addr,
            firstname=firstname,
            lastname=lastname,
            company=company,
            domain=domain,
        )


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD and capitalises)."""
    stem = domain.split(".")[0]
    return stem.capitalize() if stem else ""


@dataclass
class SyncResult:
    email: str
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    hubspot_id: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Gmail helpers  (depend on google-api-python-client)
# ---------------------------------------------------------------------------

def build_gmail_service():
    """Build an authenticated Gmail service using Application Default Credentials."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    credentials, _ = __import__("google.auth", fromlist=["default"]).default(
        scopes=["https://www.googleapis.com/auth/gmail.readonly"]
    )
    return build("gmail", "v1", credentials=credentials)


def list_recent_senders(service, hours: int = 24) -> list[SenderInfo]:
    """Return unique SenderInfo objects for emails received in the last `hours`."""
    query = f"in:inbox newer_than:{hours}h -from:me"
    results = service.users().messages().list(userId="me", q=query, maxResults=200).execute()
    messages = results.get("messages", [])

    seen: dict[str, SenderInfo] = {}
    for msg_stub in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_stub["id"], format="metadata",
            metadataHeaders=["From"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        info = SenderInfo.from_raw(raw_from)
        if info and info.email not in seen:
            seen[info.email] = info

    return list(seen.values())


# ---------------------------------------------------------------------------
# HubSpot helpers  (depend on hubspot-api-client)
# ---------------------------------------------------------------------------

def build_hubspot_client(api_key: str):
    from hubspot import HubSpot
    return HubSpot(access_token=api_key)


def find_contact_by_email(client, email: str) -> Optional[dict]:
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0].to_dict() if resp.results else None


def upsert_contact(client, sender: SenderInfo) -> SyncResult:
    from hubspot.crm.contacts import SimplePublicObjectInput, SimplePublicObjectInputForCreate

    existing = find_contact_by_email(client, sender.email)

    props_to_set: dict[str, str] = {}

    if not existing:
        # New contact
        props_to_set = {
            "email": sender.email,
            "firstname": sender.firstname,
            "lastname": sender.lastname,
            "company": sender.company,
            "hs_analytics_source": "EMAIL_MARKETING",
            "hs_analytics_source_data_1": "Gmail",
        }
        props_to_set = {k: v for k, v in props_to_set.items() if v}

        obj = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props_to_set
            )
        )
        _add_note(client, obj.id, sender.email)
        return SyncResult(email=sender.email, status="Creato", hubspot_id=obj.id)

    # Existing contact – fill in only blank fields
    existing_props = existing.get("properties", {})
    contact_id = existing["id"]

    if not existing_props.get("firstname") and sender.firstname:
        props_to_set["firstname"] = sender.firstname
    if not existing_props.get("lastname") and sender.lastname:
        props_to_set["lastname"] = sender.lastname
    if not existing_props.get("company") and sender.company:
        props_to_set["company"] = sender.company

    if props_to_set:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props_to_set),
        )

    _add_note(client, contact_id, sender.email)
    return SyncResult(
        email=sender.email,
        status="Aggiornato",
        hubspot_id=contact_id,
        reason=f"campi aggiornati: {list(props_to_set.keys()) or 'nota timeline'}",
    )


def _add_note(client, contact_id: str, sender_email: str) -> None:
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
    from hubspot.crm.associations import AssociationSpec, BatchInputPublicAssociation, PublicAssociation

    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"Email ricevuta via Gmail — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.\n"
        f"Mittente: {sender_email}\n"
        "Fonte contatto: Gmail | Tag: Inbound Gmail"
    )
    note = client.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=NoteCreate(
            properties={"hs_note_body": body, "hs_timestamp": str(ts)}
        )
    )
    client.crm.associations.batch_api.create(
        from_object_type="notes",
        to_object_type="contacts",
        batch_input_public_association=BatchInputPublicAssociation(
            inputs=[PublicAssociation(
                from_={"id": note.id},
                to={"id": contact_id},
                type="note_to_contact",
            )]
        ),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(hours: int = 24, hubspot_token: str = "") -> list[SyncResult]:
    import os

    token = hubspot_token or os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN env var not set")

    gmail_svc = build_gmail_service()
    hs_client = build_hubspot_client(token)

    senders = list_recent_senders(gmail_svc, hours=hours)
    results: list[SyncResult] = []

    for sender in senders:
        try:
            result = upsert_contact(hs_client, sender)
        except Exception as exc:
            result = SyncResult(email=sender.email, status="Ignorato", reason=str(exc))
        results.append(result)
        print(f"[{result.status}] {result.email}  →  ID {result.hubspot_id}  ({result.reason})")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts")
    parser.add_argument("--hours", type=int, default=24, help="Look-back window in hours")
    parser.add_argument("--token", default="", help="HubSpot private-app access token")
    args = parser.parse_args()
    run(hours=args.hours, hubspot_token=args.token)
