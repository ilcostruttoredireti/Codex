#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Continuously monitors the Gmail inbox and upserts sender contacts into
HubSpot, avoiding duplicates and filling in missing fields on existing records.

Output per email processed:
  CREATED / UPDATED / IGNORED  <email>  HubSpot ID: <id>
"""

import os
import re
import time
import logging
from html import unescape
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import (
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
MY_EMAIL = os.getenv("MY_EMAIL", "").lower()

# Senders to skip (notifications, internal forwarders, own address)
SKIP_DOMAINS: frozenset[str] = frozenset({
    "facebookmail.com", "google.com", "googlemail.com",
    "linkedin.com", "twitter.com", "notifications.google.com",
    "accounts.google.com", "mail.instagram.com",
})
SKIP_EMAILS: frozenset[str] = frozenset(filter(None, [MY_EMAIL]))

# Addresses that forward on behalf of real senders (inspect the snippet instead)
FORWARDING_ADDRESSES: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.getenv("FORWARDING_ADDRESSES", "redazione@latestata.it").split(",")
    if e.strip()
)

# Personal-email domains → no company inference
_PERSONAL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "libero.it", "tiscali.it", "virgilio.it", "icloud.com", "me.com",
})

# Heuristic org-name keywords
_ORG_KEYWORDS = (
    "press", "ufficio stampa", "media", "segreteria", "info",
    "redazione", "staff", "comunicazione", "running", "toyota",
    "asd", "associazione", "comune", "ente", "gazoo", "ufficio",
)


# ── Gmail authentication ──────────────────────────────────────────────────────
def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ── HubSpot client ────────────────────────────────────────────────────────────
def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


# ── Name / company helpers ─────────────────────────────────────────────────────
def _is_org_name(name: str) -> bool:
    low = name.lower()
    return any(kw in low for kw in _ORG_KEYWORDS) or name.isupper()


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0], "") if parts else ("", "")


def _company_from_domain(domain: str) -> str:
    if domain in _PERSONAL_DOMAINS:
        return ""
    stem = domain.split(".")[-2]
    return stem.replace("-", " ").replace("_", " ").title()


# ── Forwarded-email parsing ───────────────────────────────────────────────────
_FW_QUOTED_RE = re.compile(
    r'Da\s+(?:&quot;|")([^&"]+)(?:&quot;|")\s+([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)
_FW_ANGLE_RE = re.compile(
    r'(?:Da|From):\s*([^<\n]+?)\s*<([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>',
    re.IGNORECASE,
)
_FW_BARE_RE = re.compile(
    r'(?:Da|From):\s*(.+?)\s+([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)


def _extract_forwarded_sender(snippet: str) -> tuple[str | None, str | None]:
    for pattern in (_FW_QUOTED_RE, _FW_ANGLE_RE, _FW_BARE_RE):
        m = pattern.search(snippet)
        if m:
            return m.group(2).lower().strip(), unescape(m.group(1)).strip()
    return None, None


# ── Contact property builder ──────────────────────────────────────────────────
def build_contact_props(
    sender_email: str, sender_name: str
) -> dict[str, str] | None:
    """
    Return HubSpot property dict for the sender, or None if the contact
    should be skipped entirely.
    """
    email = sender_email.lower().strip()
    domain = email.split("@")[1]

    if domain in SKIP_DOMAINS or email in SKIP_EMAILS:
        return None

    name = (sender_name or "").strip()
    firstname = lastname = company = ""

    if name:
        if _is_org_name(name):
            company = name
        else:
            firstname, lastname = _split_name(name)
            company = _company_from_domain(domain)
    else:
        company = _company_from_domain(domain)

    props: dict[str, str] = {
        "email": email,
        "lifecyclestage": "lead",
        "hs_lead_status": "NEW",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    return props


# ── HubSpot upsert ────────────────────────────────────────────────────────────
def _find_contact(
    client: hubspot.Client, email: str
) -> tuple[str | None, dict]:
    search = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company"],
    )
    resp = client.crm.contacts.search_api.do_search(
        public_object_search_request=search
    )
    if resp.results:
        hit = resp.results[0]
        return hit.id, hit.properties or {}
    return None, {}


def upsert_contact(
    client: hubspot.Client, props: dict[str, str]
) -> tuple[str, str]:
    """
    Create or update a contact.  Returns (status, contact_id).
    status ∈ {"created", "updated", "ignored"}
    """
    email = props["email"]
    contact_id, existing = _find_contact(client, email)

    if contact_id is None:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return "created", result.id

    updates = {k: v for k, v in props.items() if k != "email" and not existing.get(k)}
    if updates:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return "updated", contact_id

    return "ignored", contact_id


# ── Gmail message processing ──────────────────────────────────────────────────
_FROM_HEADER_RE = re.compile(
    r'^"?([^"<]*)"?\s*<?([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>?$'
)


def extract_sender(gmail_svc, msg_id: str) -> tuple[str, str, str]:
    """
    Return (email, display_name, snippet) for a Gmail message.
    For forwarded messages from FORWARDING_ADDRESSES, parse the snippet to
    recover the original sender.
    """
    msg = (
        gmail_svc.users()
        .messages()
        .get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From"],
        )
        .execute()
    )
    snippet = msg.get("snippet", "")
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    raw_from = headers.get("From", "")

    m = _FROM_HEADER_RE.match(raw_from.strip())
    direct_email = m.group(2).lower() if m else raw_from.lower().strip()
    direct_name = m.group(1).strip() if m else ""

    if direct_email in FORWARDING_ADDRESSES:
        fw_email, fw_name = _extract_forwarded_sender(snippet)
        if fw_email:
            return fw_email, fw_name or "", snippet

    return direct_email, direct_name, snippet


# ── Gmail polling ─────────────────────────────────────────────────────────────
def _list_inbox_messages(gmail_svc, history_id: str | None) -> list[dict]:
    """
    Return new inbox messages since history_id, or the last 50 on first run.
    """
    if history_id:
        try:
            resp = (
                gmail_svc.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            messages = []
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    messages.append(added["message"])
            return messages
        except Exception:
            pass  # fall through to full list on error

    result = (
        gmail_svc.users()
        .messages()
        .list(userId="me", q="in:inbox -in:draft -in:sent", maxResults=50)
        .execute()
    )
    return result.get("messages", [])


# ── Main loop ─────────────────────────────────────────────────────────────────
def run() -> None:
    gmail_svc = get_gmail_service()
    hs_client = get_hubspot_client()

    profile = gmail_svc.users().getProfile(userId="me").execute()
    history_id: str | None = profile.get("historyId")
    first_run = True

    log.info(
        "Sync started — poll every %ds, history ID: %s", POLL_SECONDS, history_id
    )

    while True:
        try:
            messages = _list_inbox_messages(
                gmail_svc, None if first_run else history_id
            )
            first_run = False
            seen: set[str] = set()

            for meta in messages:
                msg_id = meta.get("id", "")
                try:
                    email, name, snippet = extract_sender(gmail_svc, msg_id)

                    if not email or email in seen:
                        continue
                    seen.add(email)

                    props = build_contact_props(email, name)
                    if props is None:
                        log.info("IGNORED   %-45s  (skipped)", email)
                        continue

                    status, contact_id = upsert_contact(hs_client, props)
                    log.info(
                        "%-8s  %-45s  HubSpot ID: %s",
                        status.upper(),
                        email,
                        contact_id,
                    )

                except Exception as exc:
                    log.warning("Error on message %s: %s", msg_id, exc)

            # Advance the history pointer
            if messages:
                latest = (
                    gmail_svc.users()
                    .messages()
                    .get(userId="me", id=messages[0]["id"], format="minimal")
                    .execute()
                )
                history_id = latest.get("historyId", history_id)

        except Exception as exc:
            log.error("Poll cycle error: %s", exc)

        log.info("Sleeping %ds …", POLL_SECONDS)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
