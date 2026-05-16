#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs senders as HubSpot contacts.

Usage:
    python sync.py              # run continuously (default: every 300s)
    python sync.py --once       # single pass then exit
    python sync.py --interval 60  # poll every 60 seconds

Env vars required:
    HUBSPOT_ACCESS_TOKEN  – HubSpot Private App token
    GOOGLE_CREDENTIALS    – path to OAuth credentials JSON (default: credentials.json)
"""

import os
import re
import json
import time
import base64
import logging
import argparse
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".sync_state.json")
TOKEN_FILE = Path("token.json")

# Domains/prefixes that produce automated or noise emails
IGNORED_DOMAINS = frozenset(
    [
        "facebookmail.com",
        "twitter.com",
        "linkedin.com",
        "bounces.google.com",
        "accounts.google.com",
        "notifications.google.com",
        "mail.instagram.com",
    ]
)
IGNORED_PREFIXES = frozenset(
    [
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "mailer-daemon",
        "postmaster",
        "bounce",
        "notifications",
        "support",
        "info",          # too generic; remove if you want to capture these
    ]
)


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ContactInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""


@dataclass
class SyncResult:
    status: str          # Creato / Aggiornato / Ignorato
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_ids": []}


def save_state(state: dict) -> None:
    # Keep only the last 5000 processed IDs to avoid unbounded growth
    state["processed_ids"] = state["processed_ids"][-5000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Gmail authentication & helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    creds_path = Path(os.getenv("GOOGLE_CREDENTIALS", "credentials.json"))

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, state: dict, max_results: int = 50) -> list[dict]:
    """Return unprocessed inbox messages since the last run."""
    processed = set(state["processed_ids"])
    messages = []

    if state.get("last_history_id"):
        try:
            history = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=state["last_history_id"],
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            for record in history.get("history", []):
                for m in record.get("messagesAdded", []):
                    mid = m["message"]["id"]
                    if mid not in processed:
                        messages.append({"id": mid})
            if "historyId" in history:
                state["last_history_id"] = history["historyId"]
        except Exception as exc:
            log.warning("History API fallback: %s", exc)

    if not messages:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results, q="-from:me")
            .execute()
        )
        messages = [m for m in result.get("messages", []) if m["id"] not in processed]
        profile = service.users().getProfile(userId="me").execute()
        state["last_history_id"] = profile.get("historyId")

    return messages


def parse_from_header(from_header: str) -> tuple[str, str, str]:
    """Extract (email, firstname, lastname) from a RFC 5322 From header."""
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', from_header.strip())
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip().lower()
        parts = name.split()
        firstname = parts[0].capitalize() if parts else ""
        lastname = " ".join(parts[1:]).capitalize() if len(parts) > 1 else ""
    else:
        email = from_header.strip().lower()
        local = email.split("@")[0]
        parts = re.split(r"[._\-]", local)
        firstname = parts[0].capitalize() if parts else ""
        lastname = parts[1].capitalize() if len(parts) > 1 and len(parts[1]) > 1 else ""

    return email, firstname, lastname


def domain_to_company(domain: str) -> str:
    """Best-effort company name from email domain (skips generic providers)."""
    generic = frozenset(["gmail", "yahoo", "hotmail", "outlook", "libero", "tiscali", "virgilio", "icloud"])
    parts = domain.split(".")
    if len(parts) >= 2 and parts[-2].lower() not in generic:
        return parts[-2].replace("-", " ").title()
    return ""


def extract_forwarded_sender(snippet: str) -> Optional[tuple[str, str, str]]:
    """
    Parse the original From/Da line from a forwarded-message snippet.
    Returns (email, name, raw_line) or None.
    """
    # Match Italian "Da" or English "From" forwarded header in snippet
    pattern = re.compile(
        r'(?:Da|From)[:\s]+"?([^"<\n]+?)"?\s*<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
        re.IGNORECASE,
    )
    match = pattern.search(snippet)
    if match:
        name = match.group(1).strip()
        email = match.group(2).strip().lower()
        parts = name.split()
        firstname = parts[0].capitalize() if parts else ""
        lastname = " ".join(parts[1:]).capitalize() if len(parts) > 1 else ""
        return email, firstname, lastname

    # Fallback: just find a bare email address
    bare = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', snippet)
    if bare:
        email = bare.group(0).lower()
        _, firstname, lastname = parse_from_header(email)
        return email, firstname, lastname

    return None


def should_ignore(email: str) -> bool:
    if not email or "@" not in email:
        return True
    local, domain = email.split("@", 1)
    if domain in IGNORED_DOMAINS:
        return True
    if any(local.lower().startswith(p) for p in IGNORED_PREFIXES):
        return True
    return False


def get_message_contact(service, msg_id: str, own_email: str) -> Optional[ContactInfo]:
    """Fetch one message and return a ContactInfo (or None to skip)."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata",
                 metadataHeaders=["From", "Subject"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        from_header = headers.get("From", "")
        snippet = msg.get("snippet", "")

        email, firstname, lastname = parse_from_header(from_header)
        domain = email.split("@")[1] if "@" in email else ""

        # If the message is from our own redaction/forwarding address,
        # try to find the original sender in the snippet.
        if email == own_email or email.lower().startswith("redazione@"):
            result = extract_forwarded_sender(snippet)
            if result:
                email, firstname, lastname = result
                domain = email.split("@")[1] if "@" in email else ""
            else:
                return None

        if should_ignore(email):
            return None

        company = domain_to_company(domain)
        return ContactInfo(email=email, firstname=firstname, lastname=lastname, company=company)

    except Exception as exc:
        log.error("Error reading message %s: %s", msg_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# HubSpot helpers
# ─────────────────────────────────────────────────────────────────────────────

def hs_find_contact(client, email: str) -> Optional[object]:
    try:
        req = PublicObjectSearchRequest(
            filter_groups=[FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])],
            properties=["email", "firstname", "lastname", "company"],
            limit=1,
        )
        resp = client.crm.contacts.search_api.do_search(req)
        return resp.results[0] if resp.results else None
    except Exception as exc:
        log.error("HubSpot search error (%s): %s", email, exc)
        return None


def hs_create_contact(client, info: ContactInfo) -> Optional[str]:
    props = {
        "email": info.email,
        "hs_lead_status": "NEW",
        "hs_analytics_source": "OTHER_CAMPAIGNS",  # closest bucket to "Gmail inbound"
    }
    for field in ("firstname", "lastname", "company"):
        val = getattr(info, field)
        if val:
            props[field] = val

    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
        return result.id
    except ApiException as exc:
        log.error("HubSpot create error (%s): %s", info.email, exc)
        return None


def hs_update_contact(client, contact_id: str, info: ContactInfo, existing_props: dict) -> bool:
    updates = {}
    for field in ("firstname", "lastname", "company"):
        val = getattr(info, field)
        if val and not existing_props.get(field):
            updates[field] = val

    if not updates:
        return False

    try:
        client.crm.contacts.basic_api.update(
            contact_id,
            SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error (%s): %s", contact_id, exc)
        return False


def sync_contact(client, info: ContactInfo) -> SyncResult:
    existing = hs_find_contact(client, info.email)

    if existing:
        existing_props = existing.properties or {}
        updated = hs_update_contact(client, existing.id, info, existing_props)
        status = "Aggiornato" if updated else "Ignorato"
        return SyncResult(status=status, email=info.email, hubspot_id=str(existing.id))
    else:
        cid = hs_create_contact(client, info)
        if cid:
            return SyncResult(status="Creato", email=info.email, hubspot_id=str(cid))
        return SyncResult(status="Ignorato", email=info.email, reason="Errore creazione HubSpot")


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────

def print_results(results: list[SyncResult]) -> None:
    if not results:
        return
    W = 14, 40, 16
    sep = "─" * (sum(W) + 6)
    print(f"\n{sep}")
    print(f"{'Stato':<{W[0]}} {'Email':<{W[1]}} {'ID HubSpot':<{W[2]}}")
    print(sep)
    for r in results:
        print(f"{r.status:<{W[0]}} {r.email:<{W[1]}} {r.hubspot_id or '—':<{W[2]}}")
    print(sep)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = "  |  ".join(f"{s}: {n}" for s, n in counts.items())
    print(f"Riepilogo → {summary}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────

def run(poll_interval: int = 300, once: bool = False) -> None:
    gmail = get_gmail_service()
    hs = hubspot.Client.create(access_token=os.environ["HUBSPOT_ACCESS_TOKEN"])

    # Identify the account's own address to detect forwarded emails
    profile = gmail.users().getProfile(userId="me").execute()
    own_email = profile.get("emailAddress", "").lower()
    log.info("Account Gmail: %s", own_email)

    state = load_state()

    while True:
        log.info("Controllo nuove email in arrivo…")
        messages = fetch_new_messages(gmail, state)
        log.info("%d messaggi non ancora processati", len(messages))

        seen: set[str] = set()
        results: list[SyncResult] = []

        for msg in messages:
            mid = msg["id"]
            state["processed_ids"].append(mid)

            info = get_message_contact(gmail, mid, own_email)
            if info is None:
                continue

            if info.email in seen:
                continue
            seen.add(info.email)

            result = sync_contact(hs, info)
            results.append(result)
            log.info("[%s] %s (ID: %s)", result.status, result.email, result.hubspot_id or "—")

        print_results(results)
        save_state(state)

        if once:
            break

        log.info("Prossimo controllo tra %ds…", poll_interval)
        time.sleep(poll_interval)


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--interval", type=int, default=300, help="Polling interval in seconds (default 300)")
    parser.add_argument("--once", action="store_true", help="Run a single pass then exit")
    args = parser.parse_args()
    run(poll_interval=args.interval, once=args.once)
