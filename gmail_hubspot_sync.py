"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs senders as contacts in HubSpot.
Avoids duplicates using email as unique key. Designed for scheduled execution.

Requirements:
    pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client

Environment variables:
    HUBSPOT_TOKEN       - HubSpot Private App token
    GMAIL_CREDENTIALS   - Path to Gmail OAuth2 credentials.json
    GMAIL_TOKEN         - Path to store/load Gmail OAuth2 token.json
    LOOKBACK_HOURS      - Hours to look back for new emails (default: 24)
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException as ContactApiException
from hubspot.crm.contacts.api import basic_api as contacts_basic_api

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
IGNORED_DOMAINS = {"facebookmail.com", "googlemail.com", "notifications.google.com"}
AUTOMATED_PATTERNS = [re.compile(p, re.I) for p in [
    r"^no.?reply@", r"^noreply@", r"^donotreply@",
    r"^mailer-daemon@", r"^postmaster@", r"^bounce",
]]


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    token_path = os.environ.get("GMAIL_TOKEN", "token.json")
    creds_path = os.environ.get("GMAIL_CREDENTIALS", "credentials.json")
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_recent_inbox_messages(service, hours=24):
    cutoff = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    results = []
    page_token = None
    while True:
        kwargs = dict(
            userId="me",
            q=f"in:inbox after:{cutoff} -from:me",
            maxResults=100,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        results.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return results


def get_message_sender(service, msg_id):
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject"]
    ).execute()
    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    raw_from = headers.get("from", "")
    name, email_addr = parseaddr(raw_from)
    return name.strip(), email_addr.strip().lower(), headers.get("subject", "")


def parse_company_from_domain(email_addr):
    parts = email_addr.split("@")
    if len(parts) != 2:
        return None
    domain = parts[1]
    if domain in ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "libero.it", "tin.it"):
        return None
    label = domain.split(".")[0]
    return label.replace("-", " ").replace("_", " ").title()


def is_automated(email_addr):
    domain = email_addr.split("@")[-1] if "@" in email_addr else ""
    if domain in IGNORED_DOMAINS:
        return True
    return any(p.match(email_addr) for p in AUTOMATED_PATTERNS)


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def get_hubspot_client():
    token = os.environ["HUBSPOT_TOKEN"]
    return hubspot.Client.create(access_token=token)


def find_contact_by_email(client, email_addr):
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup
    f = Filter(property_name="email", operator="EQ", value=email_addr)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return resp.results[0] if resp.results else None


def upsert_contact(client, email_addr, name="", subject=""):
    first, last = _split_name(name)
    company = parse_company_from_domain(email_addr)

    existing = find_contact_by_email(client, email_addr)
    props = {}
    if first and not (existing and existing.properties.get("firstname")):
        props["firstname"] = first
    if last and not (existing and existing.properties.get("lastname")):
        props["lastname"] = last
    if company and not (existing and existing.properties.get("company")):
        props["company"] = company

    if existing:
        contact_id = existing.id
        if props:
            from hubspot.crm.contacts import SimplePublicObjectInput
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            _add_note(client, contact_id, email_addr, subject)
            return "Aggiornato", contact_id
        return "Ignorato", contact_id
    else:
        props["email"] = email_addr
        props.setdefault("firstname", first or email_addr.split("@")[0])
        props["hs_lead_status"] = "NEW"
        obj = SimplePublicObjectInputForCreate(properties=props)
        created = client.crm.contacts.basic_api.create(simple_public_object_input_for_create=obj)
        _add_note(client, created.id, email_addr, subject)
        return "Creato", created.id


def _split_name(full_name):
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def _add_note(client, contact_id, email_addr, subject):
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput
    from hubspot.crm.associations import BatchInputPublicAssociation, PublicAssociation
    body = (
        f"Inbound Gmail — mittente: {email_addr}\n"
        f"Oggetto: {subject}\n"
        f"Data: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Tag: Inbound Gmail | Fonte: Gmail"
    )
    note = NoteInput(properties={
        "hs_note_body": body,
        "hs_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })
    note_obj = client.crm.objects.notes.basic_api.create(simple_public_object_input_for_create=note)
    assoc = PublicAssociation(
        from_={"id": note_obj.id},
        to={"id": contact_id},
        type="note_to_contact",
    )
    client.crm.associations.batch_api.create(
        from_object_type="notes",
        to_object_type="contacts",
        batch_input_public_association=BatchInputPublicAssociation(inputs=[assoc]),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    hours = int(os.environ.get("LOOKBACK_HOURS", 24))
    gmail = get_gmail_service()
    hs = get_hubspot_client()

    messages = fetch_recent_inbox_messages(gmail, hours=hours)
    log.info("Found %d inbox messages in last %dh", len(messages), hours)

    seen_emails = set()
    results = []

    for m in messages:
        try:
            name, sender_email, subject = get_message_sender(gmail, m["id"])
        except Exception as exc:
            log.warning("Could not fetch message %s: %s", m["id"], exc)
            continue

        if not sender_email or sender_email in seen_emails:
            continue
        if is_automated(sender_email):
            log.info("Skip automated sender: %s", sender_email)
            continue

        seen_emails.add(sender_email)

        try:
            status, contact_id = upsert_contact(hs, sender_email, name, subject)
        except Exception as exc:
            log.error("HubSpot error for %s: %s", sender_email, exc)
            status, contact_id = "Errore", None

        results.append((status, sender_email, contact_id))
        log.info("[%s] %s → ID %s", status, sender_email, contact_id)

    print("\n--- Risultati sincronizzazione Gmail → HubSpot ---")
    print(f"{'Stato':<12} {'Email contatto':<45} {'ID HubSpot'}")
    print("-" * 75)
    for status, email_addr, cid in results:
        print(f"{status:<12} {email_addr:<45} {cid or 'N/A'}")

    counts = {s: sum(1 for r in results if r[0] == s) for s in ("Creato", "Aggiornato", "Ignorato", "Errore")}
    print(f"\nTotale: {len(results)} contatti | " + " | ".join(f"{k}: {v}" for k, v in counts.items() if v))
    return results


if __name__ == "__main__":
    run()
