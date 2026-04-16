#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and upserts sender contacts into HubSpot.

Usage:
    export HUBSPOT_ACCESS_TOKEN=<token>
    python gmail_hubspot_sync.py

Auth files required in working directory:
    credentials.json  – OAuth client secrets from Google Cloud Console
    token.json        – generated automatically on first run
"""

import json
import logging
import os
import time
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from hubspot import HubSpot
from hubspot.crm.contacts import ApiException, SimplePublicObjectInputForCreate
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TOKEN_PATH = Path("token.json")
CREDENTIALS_PATH = Path("credentials.json")
STATE_FILE = Path(".gmail_sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))

# Senders whose local-part matches these tokens are skipped (automated mail)
_SKIP_LOCAL = {
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "posta-certificata", "postmaster", "bounce", "notifications",
    "analytics-noreply",
}
# Domains that belong to individuals, not companies
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "libero.it",
    "tiscali.it", "virgilio.it", "alice.it",
}


# ── Gmail helpers ────────────────────────────────────────────────────────────

def _build_gmail():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _fetch_message(gmail, msg_id: str) -> dict:
    return gmail.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()


def _poll(gmail, state: dict) -> list[str]:
    """Return message IDs added to INBOX since the last run."""
    last_hid = state.get("last_history_id")

    if last_hid:
        try:
            resp = gmail.users().history().list(
                userId="me",
                startHistoryId=last_hid,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            ids = [
                item["message"]["id"]
                for record in resp.get("history", [])
                for item in record.get("messagesAdded", [])
                if "INBOX" in item["message"].get("labelIds", [])
            ]
            state["last_history_id"] = resp.get("historyId", last_hid)
            return ids
        except Exception as exc:
            log.warning("History API error (%s) – falling back to list scan", exc)

    # First run: snapshot the latest messages and record the history cursor
    result = gmail.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=50
    ).execute()
    ids = [m["id"] for m in result.get("messages", [])]
    profile = gmail.users().getProfile(userId="me").execute()
    state["last_history_id"] = profile.get("historyId")
    return ids


# ── Contact extraction ───────────────────────────────────────────────────────

def _parse_sender(from_header: str) -> dict | None:
    """Return a dict with contact fields, or None if the sender should be skipped."""
    display_name, email = parseaddr(from_header)
    if not email:
        return None

    email = email.lower().strip()
    local = email.split("@")[0]

    if any(token in local for token in _SKIP_LOCAL):
        return None

    domain = email.split("@")[1] if "@" in email else ""

    firstname = lastname = ""
    if display_name:
        parts = display_name.strip().split(maxsplit=1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""

    # Derive company from the domain when it is not a personal provider
    company = ""
    if domain and domain not in _PERSONAL_DOMAINS:
        # "teatro-alkestis.it" → "Teatro Alkestis"
        company = domain.split(".")[0].replace("-", " ").title()

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ── HubSpot helpers ──────────────────────────────────────────────────────────

def _find_contact(hs: HubSpot, email: str):
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    res = hs.crm.contacts.search_api.do_search(req)
    return res.results[0] if res.results else None


def _create_contact(hs: HubSpot, info: dict) -> tuple[str, str]:
    props = {
        "email": info["email"],
        "leadsource": "Gmail",          # custom writable field
        "hs_analytics_source_data_2": "Inbound Gmail",
    }
    if info["firstname"]:
        props["firstname"] = info["firstname"]
    if info["lastname"]:
        props["lastname"] = info["lastname"]
    if info["company"]:
        props["company"] = info["company"]

    try:
        obj = hs.crm.contacts.basic_api.create(
            SimplePublicObjectInputForCreate(properties=props, associations=[])
        )
        return "Creato", obj.id
    except ApiException as exc:
        log.error("Create failed for %s: %s", info["email"], exc)
        return "Errore", ""


def _update_contact(hs: HubSpot, contact_id: str, info: dict, existing) -> tuple[str, str]:
    ep = existing.properties
    updates: dict[str, str] = {}

    if not ep.get("leadsource"):
        updates["leadsource"] = "Gmail"
    if not ep.get("hs_analytics_source_data_2"):
        updates["hs_analytics_source_data_2"] = "Inbound Gmail"
    if info["firstname"] and not ep.get("firstname"):
        updates["firstname"] = info["firstname"]
    if info["lastname"] and not ep.get("lastname"):
        updates["lastname"] = info["lastname"]
    if info["company"] and not ep.get("company"):
        updates["company"] = info["company"]

    if not updates:
        return "Ignorato", contact_id

    try:
        hs.crm.contacts.basic_api.update(
            contact_id, SimplePublicObjectInput(properties=updates)
        )
        return "Aggiornato", contact_id
    except ApiException as exc:
        log.error("Update failed for %s: %s", info["email"], exc)
        return "Errore", contact_id


def _add_note(hs: HubSpot, contact_id: str, subject: str, body: str) -> None:
    """Log a timeline note on the contact."""
    try:
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteCreate,
        )
        from datetime import datetime, timezone

        note = hs.crm.objects.notes_api.create(
            NoteCreate(
                properties={
                    "hs_note_body": body,
                    "hs_timestamp": datetime.now(timezone.utc).isoformat(),
                },
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 202,  # note → contact
                            }
                        ],
                    }
                ],
            )
        )
        log.debug("Note %s added to contact %s", note.id, contact_id)
    except Exception as exc:
        log.warning("Could not add note to %s: %s", contact_id, exc)


# ── Main loop ────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _process_message(hs: HubSpot, gmail, msg_id: str) -> dict | None:
    msg = _fetch_message(gmail, msg_id)
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    info = _parse_sender(headers.get("From", ""))
    if not info:
        return None

    existing = _find_contact(hs, info["email"])
    subject = headers.get("Subject", "(no subject)")

    if existing:
        status, cid = _update_contact(hs, existing.id, info, existing)
    else:
        status, cid = _create_contact(hs, info)

    if cid and status in ("Creato", "Aggiornato"):
        _add_note(
            hs, cid,
            subject=f"Email ricevuta: {subject}",
            body=(
                f"Email inbound ricevuta tramite Gmail.\n"
                f"Oggetto: {subject}\n"
                f"Mittente: {info['email']}\n"
                f"Tag: Inbound Gmail"
            ),
        )

    return {"status": status, "email": info["email"], "contact_id": cid}


def run() -> None:
    access_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not access_token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN env var is required")

    gmail = _build_gmail()
    hs = HubSpot(access_token=access_token)
    state = _load_state()
    processed: set[str] = set(state.get("processed_ids", []))

    log.info("Gmail→HubSpot sync started (poll every %ds)", POLL_INTERVAL)
    log.info("%-12s  %-42s  %s", "Stato", "Email", "HubSpot ID")
    log.info("-" * 72)

    while True:
        try:
            new_ids = _poll(gmail, state)
            for mid in new_ids:
                if mid in processed:
                    continue
                result = _process_message(hs, gmail, mid)
                if result:
                    log.info(
                        "%-12s  %-42s  %s",
                        result["status"], result["email"], result["contact_id"]
                    )
                processed.add(mid)

            state["processed_ids"] = list(processed)[-1000:]
            _save_state(state)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            _save_state(state)
            break
        except Exception as exc:
            log.error("Polling cycle error: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
