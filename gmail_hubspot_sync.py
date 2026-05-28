"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and upserts senders as HubSpot contacts.
"""

import os
import re
import time
import json
import logging
from datetime import datetime, timezone
from email.utils import parseaddr

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

PROCESSED_LABEL = "HubSpot-Synced"
INBOUND_TAG = "Inbound Gmail"

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
    "icloud.com", "live.com", "me.com", "aol.com", "protonmail.com",
    "libero.it", "virgilio.it", "tin.it", "tiscali.it",
}

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_gmail_service(token_path: str = "token.json",
                         creds_path: str = "credentials.json"):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    created = service.users().labels().create(userId="me", body={"name": name}).execute()
    return created["id"]


def fetch_unprocessed_messages(service, processed_label: str, max_results: int = 50):
    query = f"in:inbox -label:{processed_label}"
    resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    return resp.get("messages", [])


def get_headers(service, message_id: str) -> dict:
    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    return {h["name"]: h["value"] for h in msg["payload"]["headers"]}


def mark_processed(service, message_id: str, label_id: str):
    service.users().messages().modify(
        userId="me",
        id=message_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ---------------------------------------------------------------------------
# Sender parsing
# ---------------------------------------------------------------------------

def parse_sender(from_header: str) -> dict | None:
    name, email = parseaddr(from_header)
    email = email.lower().strip()
    if not email or "@" not in email:
        return None

    parts = name.strip().split(" ", 1) if name.strip() else []
    first_name = parts[0] if parts else ""
    last_name = parts[1] if len(parts) > 1 else ""

    domain = email.split("@")[1]
    if domain in PERSONAL_DOMAINS:
        company = ""
    else:
        # Strip TLD(s) and capitalise the company slug
        company = domain.rsplit(".", 2)[0].replace("-", " ").replace(".", " ").title()

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def build_hubspot_client() -> hubspot.Client:
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return hubspot.Client.create(access_token=token)


def find_contact(client, email: str):
    flt = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[flt])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company"],
    )
    result = client.crm.contacts.search_api.do_search(
        public_object_search_request=req
    )
    return result.results[0] if result.total > 0 else None


def create_contact(client, info: dict):
    props = {
        "email": info["email"],
        "leadsource": "Gmail",
    }
    if info["first_name"]:
        props["firstname"] = info["first_name"]
    if info["last_name"]:
        props["lastname"] = info["last_name"]
    if info["company"]:
        props["company"] = info["company"]

    return client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=props
        )
    )


def update_contact(client, contact_id: str, info: dict, existing) -> bool:
    existing_props = existing.properties
    updates: dict[str, str] = {}

    if not existing_props.get("firstname") and info["first_name"]:
        updates["firstname"] = info["first_name"]
    if not existing_props.get("lastname") and info["last_name"]:
        updates["lastname"] = info["last_name"]
    if not existing_props.get("company") and info["company"]:
        updates["company"] = info["company"]

    if updates:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
    return bool(updates)


# ---------------------------------------------------------------------------
# Core sync loop
# ---------------------------------------------------------------------------

def run_sync(poll_interval: int = 60):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_dotenv()

    log.info("=== Gmail → HubSpot sync started ===")
    gmail = build_gmail_service()
    hs = build_hubspot_client()
    label_id = get_or_create_label(gmail, PROCESSED_LABEL)
    log.info(f"Gmail processed-label id: {label_id}")

    while True:
        messages = fetch_unprocessed_messages(gmail, PROCESSED_LABEL)
        log.info(f"Trovate {len(messages)} email da processare")

        for msg in messages:
            mid = msg["id"]
            try:
                headers = get_headers(gmail, mid)
                from_hdr = headers.get("From", "")
                subject = headers.get("Subject", "(no subject)")

                info = parse_sender(from_hdr)
                if not info:
                    log.warning(f"[IGNORATO] impossibile parsare mittente: {from_hdr!r}")
                    mark_processed(gmail, mid, label_id)
                    continue

                existing = find_contact(hs, info["email"])

                if existing:
                    changed = update_contact(hs, existing.id, info, existing)
                    status = "Aggiornato" if changed else "Ignorato"
                    contact_id = existing.id
                else:
                    new_ct = create_contact(hs, info)
                    status = "Creato"
                    contact_id = new_ct.id

                log.info(
                    f"[{status}]  email={info['email']}  "
                    f"hs_id={contact_id}  subject={subject!r}"
                )
                mark_processed(gmail, mid, label_id)

            except ApiException as exc:
                log.error(f"HubSpot API error on message {mid}: {exc}")
            except Exception as exc:
                log.error(f"Unexpected error on message {mid}: {exc}", exc_info=True)

        log.info(f"Ciclo completato. Prossimo controllo tra {poll_interval}s…")
        time.sleep(poll_interval)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        help="Seconds between Gmail checks (default: 60)",
    )
    args = parser.parse_args()
    run_sync(poll_interval=args.interval)
