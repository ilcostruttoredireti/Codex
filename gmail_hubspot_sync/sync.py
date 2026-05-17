#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Continuously monitors the Gmail inbox for new emails and syncs
each sender as a contact in HubSpot (create or update, no duplicates).
"""

import json
import logging
import os
import re
import time
import argparse
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    ApiException,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "live.com", "msn.com", "aol.com", "protonmail.com", "me.com", "mac.com",
    "googlemail.com", "ymail.com", "libero.it", "virgilio.it", "tiscali.it",
    "alice.it", "tin.it", "fastwebnet.it",
}

SKIP_PATTERNS = [
    "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster",
    "bounce", "notification", "facebookmail", "mailchimp", "amazonses",
]

STATE_FILE = Path("state.json")
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Italian/English forwarded message patterns: "Da: Name <email>" or "From: Name <email>"
FORWARDED_SENDER_RE = re.compile(
    r"(?:Da|From):\s*(?:\"?([^\"<\n]+?)\"?\s*)?<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, state: dict, max_results: int = 50) -> list[dict]:
    processed = set(state.get("processed_ids", []))
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], q="is:unread", maxResults=max_results)
            .execute()
        )
    except Exception as e:
        logger.error("Gmail list error: %s", e)
        return []

    new_ids = [m["id"] for m in result.get("messages", []) if m["id"] not in processed]
    full_messages = []
    for msg_id in new_ids:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", messageId=msg_id, format="full")
                .execute()
            )
            full_messages.append(msg)
        except Exception as e:
            logger.warning("Could not fetch message %s: %s", msg_id, e)
    return full_messages


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _plain_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        import base64
        data = payload.get("body", {}).get("data", "")
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        text = _plain_body(part)
        if text:
            return text
    return ""


def extract_senders(message: dict) -> list[dict]:
    """
    Returns a list of sender dicts extracted from a message.
    Includes the direct From: sender and any senders embedded in
    forwarded bodies (Italian 'Da:' or English 'From:' headers).
    """
    senders = []

    # --- Direct From header ---
    from_header = _header(message, "from")
    if from_header:
        direct = _parse_address(from_header)
        if direct:
            senders.append(direct)

    # --- Forwarded sender(s) embedded in body ---
    body = _plain_body(message.get("payload", {}))
    for match in FORWARDED_SENDER_RE.finditer(body):
        name_part = (match.group(1) or "").strip().strip('"')
        email_part = match.group(2).strip().lower()
        sender = _parse_address(f"{name_part} <{email_part}>")
        if sender:
            senders.append(sender)

    return senders


def _parse_address(raw: str) -> dict | None:
    name, email = parseaddr(raw)
    email = email.lower().strip()
    if not email or "@" not in email:
        return None

    if any(p in email for p in SKIP_PATTERNS):
        return None

    domain = email.split("@")[1]
    name = name.strip().strip('"')
    parts = name.split() if name else []
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    company = ""
    if domain not in PERSONAL_DOMAINS:
        # e.g. nextpress.it → Nextpress
        company = domain.split(".")[0].replace("-", " ").title()

    return {"email": email, "first_name": first, "last_name": last,
            "company": company, "domain": domain}


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def get_hubspot_client() -> hubspot.Client:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN") or os.environ.get("HUBSPOT_API_KEY")
    if not token:
        raise ValueError("Set HUBSPOT_ACCESS_TOKEN (or HUBSPOT_API_KEY) env var")
    return hubspot.Client.create(access_token=token)


def find_contact(hs: hubspot.Client, email: str):
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source_data_1"],
    )
    result = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    return result.results[0] if result.total > 0 else None


def sync_contact(hs: hubspot.Client, sender: dict) -> tuple[str, str]:
    """
    Returns (status, hubspot_id) where status is one of:
    'Creato', 'Aggiornato', 'Ignorato', 'Errore'
    """
    try:
        existing = find_contact(hs, sender["email"])
    except ApiException as e:
        logger.error("HubSpot search failed for %s: %s", sender["email"], e)
        return "Errore", ""

    props = {"email": sender["email"], "lifecyclestage": "lead"}
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    if existing:
        ep = existing.properties
        # Only fill genuinely missing fields; never overwrite existing data
        update = {k: v for k, v in props.items()
                  if k != "email" and not ep.get(k) and v}
        if not update:
            return "Ignorato", existing.id
        try:
            hs.crm.contacts.basic_api.update(
                contact_id=existing.id,
                simple_public_object_input=SimplePublicObjectInput(properties=update),
            )
            return "Aggiornato", existing.id
        except ApiException as e:
            logger.error("HubSpot update error for %s: %s", sender["email"], e)
            return "Errore", existing.id
    else:
        try:
            created = hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return "Creato", created.id
        except ApiException as e:
            logger.error("HubSpot create error for %s: %s", sender["email"], e)
            return "Errore", ""


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Core sync logic (single pass)
# ---------------------------------------------------------------------------

def run_once(gmail_svc, hs_client) -> list[dict]:
    state = load_state()
    messages = fetch_new_messages(gmail_svc, state)
    logger.info("Found %d new messages to process", len(messages))

    seen_emails: set[str] = set()
    results: list[dict] = []
    processed_ids = list(state.get("processed_ids", []))

    for msg in messages:
        msg_id = msg["id"]
        senders = extract_senders(msg)
        processed_ids.append(msg_id)

        for sender in senders:
            email = sender["email"]
            if email in seen_emails:
                continue
            seen_emails.add(email)

            status, contact_id = sync_contact(hs_client, sender)
            icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️", "Errore": "❌"}.get(status, "?")
            logger.info("%s %-10s | %-45s | ID: %s", icon, status, email, contact_id)

            results.append({
                "stato": status,
                "email": email,
                "hubspot_id": contact_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    # Keep last 10 000 IDs to bound memory
    state["processed_ids"] = processed_ids[-10_000:]
    save_state(state)
    return results


def print_report(results: list[dict]):
    if not results:
        print("\nNessun contatto da processare.")
        return
    print(f"\n{'='*65}")
    print(f"{'STATO':<12} {'EMAIL':<42} {'HUBSPOT ID'}")
    print(f"{'='*65}")
    for r in results:
        print(f"{r['stato']:<12} {r['email']:<42} {r['hubspot_id']}")
    print(f"{'='*65}")
    counts = {}
    for r in results:
        counts[r["stato"]] = counts.get(r["stato"], 0) + 1
    summary = "  ".join(f"{k}: {v}" for k, v in counts.items())
    print(f"Totale: {len(results)}  |  {summary}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--interval", type=int, default=300,
        help="Polling interval in seconds (default: 300). Ignored with --once.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single sync pass and exit.",
    )
    parser.add_argument(
        "--max", type=int, default=50,
        help="Max new messages to process per pass (default: 50).",
    )
    args = parser.parse_args()

    gmail_svc = get_gmail_service()
    hs_client = get_hubspot_client()

    if args.once:
        results = run_once(gmail_svc, hs_client)
        print_report(results)
        return

    logger.info("Starting continuous Gmail → HubSpot sync (interval: %ds)", args.interval)
    while True:
        try:
            results = run_once(gmail_svc, hs_client)
            print_report(results)
        except Exception as e:
            logger.error("Sync pass failed: %s", e)
        logger.info("Next check in %ds…", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
