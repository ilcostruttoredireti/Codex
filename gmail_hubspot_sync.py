#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for incoming emails and automatically creates/updates
contacts in HubSpot CRM. Handles forwarded press-release chains by parsing
the Italian 'Da "Name" email' and English 'From: "Name" <email>' patterns.

Usage:
    python gmail_hubspot_sync.py             # one-shot sync
    python gmail_hubspot_sync.py --continuous # poll every POLL_INTERVAL_SECONDS
    python gmail_hubspot_sync.py --dry-run    # preview without writing to HubSpot
"""

import argparse
import json
import logging
import os
import re
import time
from base64 import urlsafe_b64decode
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Google API ────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── HubSpot API ───────────────────────────────────────────────────────────────
from hubspot import HubSpot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (all overridable via .env)
# ─────────────────────────────────────────────────────────────────────────────
GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE       = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN    = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE       = os.getenv("SYNC_STATE_FILE", "sync_state.json")
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))   # 5 min default
MAX_THREADS      = int(os.getenv("MAX_THREADS_PER_PASS", "200"))
DRY_RUN          = os.getenv("DRY_RUN", "false").lower() == "true"
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO")

# The operator's own email addresses — never sync as contacts
OWN_EMAILS: set[str] = set(filter(None, [
    e.strip().lower()
    for e in os.getenv("OWN_EMAILS", "").split(",")
]))

# Domains that produce system/no-reply mail — always skip
SKIP_DOMAINS = {
    "accounts.google.com",
    "mailer-daemon.googlemail.com",
    "notifications.github.com",
    "noreply.github.com",
    "bounce.linkedin.com",
    "mailer.linkedin.com",
    "facebookmail.com",
    "amazonses.com",
}

# Local-part prefixes that identify automated senders
SKIP_PREFIXES = {"no-reply", "noreply", "mailer-daemon", "postmaster", "bounce", "donotreply"}

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hs_sync")


# ─────────────────────────────────────────────────────────────────────────────
# Gmail helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_gmail_service():
    """Return an authenticated Gmail API service, prompting OAuth if needed."""
    creds: Optional[Credentials] = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def iter_inbox_threads(service, max_results: int = MAX_THREADS):
    """
    Yield Gmail thread IDs from the inbox.
    Excludes drafts and sent mail by default.
    """
    page_token = None
    fetched = 0
    query = "in:inbox -from:me"
    while fetched < max_results:
        batch = min(max_results - fetched, 100)
        params: dict = dict(userId="me", q=query, maxResults=batch)
        if page_token:
            params["pageToken"] = page_token
        resp = service.users().threads().list(**params).execute()
        for thread in resp.get("threads", []):
            yield thread["id"]
            fetched += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def get_thread_detail(service, thread_id: str) -> list[dict]:
    """
    Return a list of message dicts for a thread.
    Each dict has: sender_raw, subject, snippet, body.
    Uses metadata format to avoid fetching large bodies when possible,
    then falls back to getting plaintext body via full format only
    for the first message (which carries the original sender).
    """
    data = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()
    messages = []
    for msg in data.get("messages", []):
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        messages.append({
            "id": msg["id"],
            "sender_raw": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "snippet": msg.get("snippet", ""),
            "body": _extract_plaintext(msg),
        })
    return messages


def _extract_plaintext(msg: dict) -> str:
    """Walk a Gmail message payload tree and return the plain-text body."""
    payload = msg.get("payload", {})
    return _walk_parts(payload)


def _walk_parts(part: dict) -> str:
    mime = part.get("mimeType", "")
    sub_parts = part.get("parts", [])
    if sub_parts:
        for sub in sub_parts:
            text = _walk_parts(sub)
            if text:
                return text
    if mime == "text/plain":
        data = part.get("body", {}).get("data", "")
        if data:
            return urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Sender / contact extraction
# ─────────────────────────────────────────────────────────────────────────────

# RFC 2822 "Display Name <email>" or bare email
_RFC_FROM = re.compile(
    r'"?([^"<\n]*?)"?\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>'
    r'|([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)

# Forwarded header in Italian ("Da") and English ("From") — both inline and as body lines
_FWD_HEADER = re.compile(
    r'(?:^|\n)\s*(?:Da|From)[:\s]+"?([^"\n<>]*?)"?\s*'
    r'<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
    re.IGNORECASE | re.MULTILINE,
)


def parse_rfc_address(raw: str) -> tuple[str, str]:
    """Return (display_name, email) from a raw RFC-2822 address string."""
    m = _RFC_FROM.search(raw.strip())
    if m:
        name = (m.group(1) or "").strip().strip('"')
        email = (m.group(2) or m.group(3) or "").strip().lower()
        return name, email
    return "", ""


def extract_forwarded_senders(text: str) -> list[tuple[str, str]]:
    """
    Scan plain-text body for original senders in forwarded messages.
    Returns deduplicated [(name, email), ...] in order of appearance.
    Handles the Italian press-release forwarding pattern:
        Da "Luana Pioppi" luanapioppi@gmail.com
        Da press@enotecaregionalemango.it
    """
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for m in _FWD_HEADER.finditer(text):
        name = (m.group(1) or "").strip().strip('"')
        email = (m.group(2) or "").strip().lower()
        if email and email not in seen:
            seen.add(email)
            results.append((name, email))
    return results


def is_skippable(email: str, own_emails: set[str] = OWN_EMAILS) -> bool:
    """Return True if this address should never be synced to HubSpot."""
    if not email or "@" not in email:
        return True
    local, domain = email.rsplit("@", 1)
    if domain in SKIP_DOMAINS:
        return True
    if any(local.lower().startswith(p) for p in SKIP_PREFIXES):
        return True
    if email.lower() in own_emails:
        return True
    return False


def domain_to_company(domain: str) -> str:
    """
    Derive a human-readable company name from an email domain.
    e.g.  enotecaregionalemango.it → Enotecaregionalemango
          t-racing.it              → T-Racing
    """
    parts = domain.split(".")
    # Drop TLD (and country code if 3+ parts)
    core_parts = parts[:-2] if len(parts) > 2 else parts[:-1]
    name = ".".join(core_parts) if core_parts else parts[0]
    return name.replace("-", " ").replace("_", " ").title()


def split_full_name(full: str) -> tuple[str, str]:
    """'Mario Rossi' → ('Mario', 'Rossi'). Single word → (word, '')."""
    parts = full.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0], "") if parts else ("", "")


# ─────────────────────────────────────────────────────────────────────────────
# HubSpot helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_hs_client() -> HubSpot:
    if not HUBSPOT_TOKEN:
        raise RuntimeError(
            "HUBSPOT_ACCESS_TOKEN is not set. "
            "Add it to your .env file or environment variables."
        )
    return HubSpot(access_token=HUBSPOT_TOKEN)


def hs_find_contact(client: HubSpot, email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the record dict or None."""
    filt = Filter(property_name="email", operator="EQ", value=email)
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[filt])],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(req)
        if resp.results:
            return resp.results[0].to_dict()
    except ApiException as exc:
        log.warning("HubSpot search failed for %s: %s", email, exc)
    return None


def hs_create_contact(client: HubSpot, props: dict) -> Optional[str]:
    """Create a new HubSpot contact. Returns the new contact ID or None."""
    try:
        resp = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return str(resp.id)
    except ApiException as exc:
        log.error("HubSpot create failed for %s: %s", props.get("email"), exc)
    return None


def hs_update_contact(client: HubSpot, contact_id: str, props: dict) -> bool:
    """Update fields on an existing HubSpot contact. Returns success bool."""
    try:
        client.crm.contacts.basic_api.update(
            contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update failed for id=%s: %s", contact_id, exc)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Core sync logic
# ─────────────────────────────────────────────────────────────────────────────

def sync_one_contact(
    client: HubSpot,
    name: str,
    email: str,
) -> dict:
    """
    Look up *email* in HubSpot.
    - If not found: create with all available fields.
    - If found:     fill in any blank fields (non-destructive update).

    Returns:
        { "status": "created" | "updated" | "ignored" | "error",
          "email": str,
          "id": str | None }
    """
    domain = email.split("@")[1] if "@" in email else ""
    first, last = split_full_name(name) if name else ("", "")
    company = domain_to_company(domain) if domain else ""

    existing = hs_find_contact(client, email)

    # ── UPDATE path ──────────────────────────────────────────────────────────
    if existing:
        contact_id = str(existing["id"])
        ep = existing.get("properties") or {}
        updates: dict[str, str] = {}

        if not ep.get("firstname") and first:
            updates["firstname"] = first
        if not ep.get("lastname") and last:
            updates["lastname"] = last
        if not ep.get("company") and company:
            updates["company"] = company
        # Stamp source tag if missing
        if not ep.get("leadsource"):
            updates["leadsource"] = "Gmail"

        if updates:
            if not DRY_RUN:
                hs_update_contact(client, contact_id, updates)
            status = "updated"
        else:
            status = "ignored"

        log.info("[%-8s]  %-45s  id=%s", status.upper(), email, contact_id)
        return {"status": status, "email": email, "id": contact_id}

    # ── CREATE path ──────────────────────────────────────────────────────────
    props: dict[str, str] = {"email": email, "leadsource": "Gmail"}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company

    contact_id = None
    if not DRY_RUN:
        contact_id = hs_create_contact(client, props)

    tag = "dry-run" if DRY_RUN else (contact_id or "error")
    status = "created" if (DRY_RUN or contact_id) else "error"
    log.info("[%-8s]  %-45s  id=%s", status.upper(), email, tag)
    return {"status": status, "email": email, "id": contact_id}


def process_thread(client: HubSpot, messages: list[dict]) -> list[dict]:
    """
    Extract all unique contacts from a thread's messages and sync each one.
    Handles both direct senders and original senders in forwarded bodies.
    """
    candidates: list[tuple[str, str]] = []   # (name, email)

    for msg in messages:
        # 1. Direct envelope sender
        name, email = parse_rfc_address(msg["sender_raw"])
        if email and not is_skippable(email):
            candidates.append((name, email))

        # 2. Original sender extracted from forwarded body text
        body_text = msg.get("body") or msg.get("snippet", "")
        for fwd_name, fwd_email in extract_forwarded_senders(body_text):
            if not is_skippable(fwd_email):
                candidates.append((fwd_name, fwd_email))

    # Deduplicate by email, preserving first occurrence
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for name, email in candidates:
        if email not in seen:
            seen.add(email)
            unique.append((name, email))

    results = []
    for name, email in unique:
        results.append(sync_one_contact(client, name, email))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# State persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"processed_threads": [], "last_sync": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def run_sync(continuous: bool = False) -> list[dict]:
    gmail  = build_gmail_service()
    hs     = build_hs_client()
    state  = load_state()
    done   = set(state.get("processed_threads", []))
    all_results: list[dict] = []

    while True:
        log.info("─── Sync pass started ───")
        pass_results: list[dict] = []
        new_done: set[str] = set()

        for thread_id in iter_inbox_threads(gmail):
            if thread_id in done:
                continue

            try:
                messages = get_thread_detail(gmail, thread_id)
            except Exception as exc:
                log.warning("Could not fetch thread %s: %s", thread_id, exc)
                new_done.add(thread_id)
                continue

            if not messages:
                new_done.add(thread_id)
                continue

            thread_results = process_thread(hs, messages)
            pass_results.extend(thread_results)
            new_done.add(thread_id)

        done.update(new_done)
        state["processed_threads"] = sorted(done)
        state["last_sync"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

        created = sum(1 for r in pass_results if r["status"] == "created")
        updated = sum(1 for r in pass_results if r["status"] == "updated")
        ignored = sum(1 for r in pass_results if r["status"] == "ignored")
        errors  = sum(1 for r in pass_results if r["status"] == "error")
        log.info(
            "Pass complete — created: %d | updated: %d | ignored: %d | errors: %d",
            created, updated, ignored, errors,
        )

        # Print per-email table
        if pass_results:
            print(f"\n{'STATUS':<10} {'EMAIL':<50} {'HUBSPOT ID'}")
            print("-" * 85)
            for r in pass_results:
                print(f"{r['status'].upper():<10} {r['email']:<50} {r['id'] or '-'}")
            print()

        all_results.extend(pass_results)

        if not continuous:
            break

        log.info("Sleeping %d s before next pass …", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)

    return all_results


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sync Gmail sender contacts into HubSpot CRM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--continuous", action="store_true",
        help="Keep polling (interval set by POLL_INTERVAL_SECONDS env var, default 300 s).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate without writing anything to HubSpot.",
    )
    args = parser.parse_args()

    if args.dry_run:
        os.environ["DRY_RUN"] = "true"
        DRY_RUN = True  # noqa: F811 — re-bind module-level flag

    run_sync(continuous=args.continuous)
