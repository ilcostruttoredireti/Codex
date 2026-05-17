#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Polls the Gmail INBOX for new incoming messages and upserts each sender
as a HubSpot contact.  Gmail History API tracks progress so every message
is processed exactly once, even across restarts.

Quickstart
----------
1. Enable the Gmail API and create an OAuth 2.0 Desktop credential at
   console.cloud.google.com. Download it as credentials.json.
2. Create a HubSpot Private App with Contacts read/write and CRM Engagements
   write scope. Copy the access token.
3. cp .env.example .env  →  fill in HUBSPOT_ACCESS_TOKEN
4. pip install -r requirements.txt
5. python gmail_hubspot_sync.py   (first run opens browser for Gmail auth)

Output per email
----------------
  ✚ CREATED  | sender@example.com                       | HubSpot ID: 12345
  ↺ UPDATED  | contact@corp.com                         | HubSpot ID: 67890
  · IGNORED  | already-complete@partner.io              | HubSpot ID: 11111
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional

import httpx
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES   = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE   = "https://api.hubapi.com"
POLL_INTERVAL  = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
TOKEN_FILE     = os.getenv("GMAIL_TOKEN_FILE", "token.json")
CREDS_FILE     = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
HS_TOKEN       = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE     = os.getenv("STATE_FILE", ".sync_state.json")

# Consumer email domains — no company name derived from these
CONSUMER_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com",
    "outlook.com", "icloud.com", "me.com", "live.com", "aol.com",
    "protonmail.com", "proton.me", "mail.com",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds: Optional[Credentials] = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_FILE):
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {CREDS_FILE}\n"
                    "Download it from console.cloud.google.com → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_new_messages(service, state: dict) -> tuple[list[dict], str]:
    """
    Return (full_messages, updated_history_id).

    Uses Gmail History API when a historyId is stored; falls back to
    querying the last hour's INBOX messages on first run or after the
    historyId expires (>30 days old).
    """
    history_id: Optional[str] = state.get("history_id")
    raw_messages: list[dict] = []

    if history_id:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    raw_messages.append(added["message"])
            new_history_id = resp.get("historyId", history_id)
        except HttpError as exc:
            if exc.status_code == 404:
                log.warning("History ID expired — falling back to last-hour scan.")
                history_id = None
            else:
                raise

    if not history_id:
        profile = service.users().getProfile(userId="me").execute()
        new_history_id = profile["historyId"]
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], q="newer_than:1h")
            .execute()
        )
        raw_messages = result.get("messages", [])

    processed_ids: set[str] = set(state.get("processed_ids", []))
    full_messages: list[dict] = []
    for m in raw_messages:
        mid = m["id"]
        if mid in processed_ids:
            continue
        try:
            full = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From", "Subject"],
                )
                .execute()
            )
            full_messages.append(full)
        except HttpError:
            log.warning("Could not fetch message %s — skipping.", mid)

    return full_messages, new_history_id


def _domain_to_company(domain: str) -> str:
    """'acme-corp.com' → 'Acme Corp'."""
    name = domain.split(".")[0]
    return re.sub(r"[-_]", " ", name).title()


def parse_sender(msg: dict) -> Optional[dict]:
    """Return a sender dict, or None if the message should be skipped."""
    from_header = _header(msg, "From")
    if not from_header:
        return None
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()
    if not email or "@" not in email:
        return None
    # Skip no-reply / automated senders
    local = email.split("@")[0]
    if re.match(r"no.?reply|noreply|mailer-daemon|postmaster|bounce", local):
        return None

    domain = email.split("@")[1]
    display_name = display_name.strip().strip('"')
    parts = display_name.split(None, 1) if display_name else []
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    company = "" if domain in CONSUMER_DOMAINS else _domain_to_company(domain)

    return {
        "email": email,
        "firstname": first,
        "lastname": last,
        "domain": domain,
        "company": company,
        "subject": _header(msg, "Subject"),
        "message_id": msg["id"],
    }


# ── HubSpot ───────────────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HS_TOKEN}", "Content-Type": "application/json"}


def hs_search_contact(email: str) -> Optional[dict]:
    """Return existing contact record or None."""
    body = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    r = httpx.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=body,
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    r = httpx.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    r = httpx.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hs_log_email_engagement(contact_id: str, sender: dict) -> None:
    """Add an 'email received' note to the contact's HubSpot timeline."""
    ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = {
        "engagement": {"active": True, "type": "NOTE", "timestamp": ts_ms},
        "associations": {"contactIds": [int(contact_id)]},
        "metadata": {
            "body": (
                f"<b>Inbound email received</b><br>"
                f"From: {sender['firstname']} {sender['lastname']} &lt;{sender['email']}&gt;<br>"
                f"Subject: {sender.get('subject', '(no subject)')}<br>"
                f"Tag: Inbound Gmail | Source: Gmail"
            )
        },
    }
    r = httpx.post(
        f"{HUBSPOT_BASE}/engagements/v1/engagements",
        headers=_hs_headers(),
        json=body,
        timeout=15,
    )
    if r.status_code not in (200, 201):
        log.warning("Could not create engagement for %s: %s", contact_id, r.text[:200])


def upsert_contact(sender: dict) -> tuple[str, str]:
    """
    Create or update a HubSpot contact from a parsed sender.
    Returns (status, contact_id) where status ∈ {'created', 'updated', 'ignored'}.
    """
    existing = hs_search_contact(sender["email"])

    props: dict[str, str] = {"email": sender["email"]}
    if sender.get("firstname"):
        props["firstname"] = sender["firstname"]
    if sender.get("lastname"):
        props["lastname"] = sender["lastname"]
    if sender.get("company"):
        props["company"] = sender["company"]

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        # Only patch genuinely empty fields (don't overwrite existing data)
        patch = {
            k: v
            for k, v in props.items()
            if k != "email" and not existing_props.get(k)
        }
        if patch:
            hs_update_contact(contact_id, patch)
            return "updated", contact_id
        return "ignored", contact_id

    result = hs_create_contact(props)
    return "created", result["id"]


# ── Main loop ─────────────────────────────────────────────────────────────────

ICON = {"created": "✚", "updated": "↺", "ignored": "·"}


def process_cycle(service, state: dict) -> dict:
    messages, new_history_id = fetch_new_messages(service, state)
    processed_ids: set[str] = set(state.get("processed_ids", []))

    log.info("Found %d new message(s) to process.", len(messages))

    for msg in messages:
        mid = msg["id"]
        sender = parse_sender(msg)
        if not sender:
            processed_ids.add(mid)
            continue

        log.info("Processing from %s <%s>", sender["firstname"] or "—", sender["email"])
        try:
            status, contact_id = upsert_contact(sender)
            try:
                hs_log_email_engagement(contact_id, sender)
            except Exception as eng_exc:
                log.warning("Engagement logging failed: %s", eng_exc)

            print(
                f"  {ICON[status]} {status.upper():7s} | "
                f"{sender['email']:45s} | HubSpot ID: {contact_id}"
            )
        except httpx.HTTPStatusError as exc:
            log.error("HubSpot error for %s: %s", sender["email"], exc.response.text[:300])
        except Exception as exc:
            log.error("Unexpected error for %s: %s", sender["email"], exc)

        processed_ids.add(mid)

    state["history_id"] = new_history_id
    # Cap stored IDs to avoid unbounded growth
    state["processed_ids"] = list(processed_ids)[-5_000:]
    return state


def main() -> None:
    if not HS_TOKEN:
        raise SystemExit(
            "HUBSPOT_ACCESS_TOKEN is not set.\n"
            "Create a Private App in HubSpot and export its token to .env."
        )

    log.info("Starting Gmail → HubSpot sync  (poll every %ds)", POLL_INTERVAL)
    service = get_gmail_service()
    state = load_state()

    while True:
        try:
            state = process_cycle(service, state)
            save_state(state)
        except HttpError as exc:
            log.error("Gmail API error: %s", exc)
        except httpx.RequestError as exc:
            log.error("Network error: %s", exc)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
