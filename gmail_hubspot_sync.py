#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitors the Gmail inbox, extracts sender information from every new email,
and syncs contacts to HubSpot (create new / update existing).

Usage
-----
    # First-time OAuth setup:
    python gmail_hubspot_sync.py --setup

    # Sync emails from the last 24 hours (default):
    python gmail_hubspot_sync.py

    # Sync emails from a custom window:
    python gmail_hubspot_sync.py --hours 48

    # Preview only, no writes:
    python gmail_hubspot_sync.py --dry-run

    # Continuous polling loop (every 5 minutes):
    python gmail_hubspot_sync.py --watch --interval 300

Requirements
------------
    pip install -r requirements.txt

Environment variables (see .env.example)
-----------------------------------------
    HUBSPOT_ACCESS_TOKEN   HubSpot private-app access token (required)
    GMAIL_CREDENTIALS_FILE Path to credentials.json from Google Cloud Console
                           (default: credentials.json)
    GMAIL_TOKEN_FILE       Path to store the OAuth token (default: token.json)
    GMAIL_ACCOUNT          Gmail address to monitor; used only as skip-filter
                           (default: inferred from the authorised token)
    LOOKBACK_HOURS         Default lookback window in hours (default: 24)
    STATE_FILE             JSON file to track processed message IDs
                           (default: processed_emails.json)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hubspot_sync")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE = os.getenv("STATE_FILE", "processed_emails.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
GMAIL_ACCOUNT = os.getenv("GMAIL_ACCOUNT", "").lower()

# Sender addresses / domain patterns to always ignore
SKIP_ADDRESSES: set[str] = {
    "mailer-daemon@googlemail.com",
    "noreply@google.com",
    "no-reply@google.com",
    "analytics-noreply@google.com",
}
SKIP_LOCAL_PATTERNS: tuple[str, ...] = (
    "mailer-daemon",
    "noreply",
    "no-reply",
    "postmaster",
    "notification",
    "notifications",
    "bounce",
    "posta-certificata",
    "do-not-reply",
)
SKIP_DOMAINS: set[str] = {
    "googlemail.com",
    "google.com",
    "legalmail.it",
    "facebookmail.com",
    "bounce.mail.",
    "amazonses.com",
    "sendgrid.net",
}

# Common free email provider domains – still include the contact, but
# don't use the domain as a company name.
FREE_EMAIL_DOMAINS: set[str] = {
    "gmail.com", "yahoo.it", "yahoo.com", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "libero.it", "virgilio.it", "alice.it",
    "tiscali.it", "tin.it",
}

# HubSpot lead source closest to "inbound email"
HUBSPOT_LEAD_SOURCE = "EMAIL_MARKETING"
INBOUND_GMAIL_TAG = "Inbound Gmail"
INBOUND_GMAIL_NOTE_BODY = (
    "Contatto acquisito automaticamente dalla casella Gmail.\n"
    "Fonte: Inbound Gmail\n"
    "Tag: Inbound Gmail"
)


# ---------------------------------------------------------------------------
# Helpers – text/name parsing
# ---------------------------------------------------------------------------

def parse_sender(raw_sender: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a raw From header."""
    display_name, email_address = parseaddr(raw_sender)
    email_address = email_address.strip().lower()
    display_name = display_name.strip().strip('"').strip("'")
    return display_name, email_address


def split_name(display_name: str) -> tuple[str, str]:
    """
    Split a display name into (first_name, last_name).
    Handles single-word names, 'First Last', and 'Last, First' formats.
    """
    if not display_name:
        return "", ""
    if "," in display_name:
        parts = [p.strip() for p in display_name.split(",", 1)]
        return parts[1], parts[0]
    parts = display_name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def domain_from_email(email: str) -> str:
    """Extract the domain part of an email address."""
    if "@" in email:
        return email.split("@", 1)[1].lower()
    return ""


def company_from_domain(domain: str) -> str:
    """
    Derive a human-readable company name from a domain.
    Returns an empty string for free/generic providers.
    """
    if not domain or domain in FREE_EMAIL_DOMAINS:
        return ""

    # Strip subdomain (e.g. mail.example.com → example.com)
    parts = domain.split(".")
    if len(parts) > 2:
        domain = ".".join(parts[-2:])

    # Remove TLD
    name = re.sub(r"\.[a-z]{2,6}$", "", domain)

    # Expand camelCase / known suffixes into words
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    # Insert space before known Italian/English suffixes attached to words
    for suffix in ("srl", "spa", "snc", "sas", "ltd", "llc", "inc", "gmbh"):
        name = re.sub(rf"(?i)({suffix})$", r" \1", name.strip())

    # Replace hyphens/underscores/dots with spaces
    name = re.sub(r"[-_.]", " ", name)

    return name.strip().title()


def should_skip(email: str, display_name: str) -> bool:
    """Return True if this sender should never be synced to HubSpot."""
    email = email.lower()
    if not email or "@" not in email:
        return True
    local, domain = email.split("@", 1)

    if email in SKIP_ADDRESSES:
        return True
    if domain in SKIP_DOMAINS or any(domain.endswith(d) for d in SKIP_DOMAINS):
        return True
    if any(local.startswith(p) for p in SKIP_LOCAL_PATTERNS):
        return True
    return False


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------

def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds: Optional[Credentials] = None
    token_path = Path(TOKEN_FILE)
    creds_path = Path(CREDENTIALS_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → OAuth 2.0 Client IDs."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, name: str) -> str:
    """Return the label ID for *name*, creating it if absent."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"].lower() == name.lower():
            return lbl["id"]
    body = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = service.users().labels().create(userId="me", body=body).execute()
    log.info("Created Gmail label '%s' (id=%s)", name, created["id"])
    return created["id"]


def fetch_inbox_messages(service, since_hours: int) -> list[dict]:
    """Return all inbox messages received in the last *since_hours* hours."""
    # Gmail date filter is date-granular; use newer_than for hour granularity
    query = f"in:inbox newer_than:{since_hours}h"
    messages: list[dict] = []
    page_token = None

    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return messages


def get_message_headers(service, msg_id: str) -> dict:
    """Fetch only the headers we need for a given message ID."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    headers["threadId"] = msg.get("threadId", "")
    headers["labelIds"] = msg.get("labelIds", [])
    return headers


# ---------------------------------------------------------------------------
# HubSpot client
# ---------------------------------------------------------------------------

def get_hubspot_client():
    """Return an authenticated HubSpot API client."""
    import hubspot as hs
    if not HUBSPOT_TOKEN:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN is not set. "
            "Create a private app in HubSpot and copy its token into .env."
        )
    return hs.Client.create(access_token=HUBSPOT_TOKEN)


def search_contact_by_email(hs_client, email: str) -> Optional[dict]:
    """Return the first matching HubSpot contact for *email*, or None."""
    from hubspot.crm.contacts import PublicObjectSearchRequest

    req = PublicObjectSearchRequest(
        filter_groups=[{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    resp = hs_client.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.results:
        return resp.results[0].to_dict()
    return None


def build_contact_properties(
    email: str,
    firstname: str,
    lastname: str,
    company: str,
    *,
    existing: Optional[dict] = None,
) -> dict:
    """
    Build a properties dict for HubSpot create/update.
    When *existing* is provided, only fill in blank fields.
    """
    props: dict[str, str] = {}

    def _set(field: str, value: str) -> None:
        """Only set if the new value is non-empty, and either no existing record
        or the existing field is blank."""
        if not value:
            return
        if existing is None:
            props[field] = value
            return
        current = (existing.get("properties") or {}).get(field, "")
        if not current:
            props[field] = value

    _set("firstname", firstname)
    _set("lastname", lastname)
    _set("company", company)
    _set("leadsource", HUBSPOT_LEAD_SOURCE)

    # Always set email on create; never overwrite on update
    if existing is None:
        props["email"] = email

    return props


def create_contact(hs_client, props: dict) -> dict:
    """Create a new HubSpot contact and return the created object dict."""
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    body = SimplePublicObjectInputForCreate(properties=props)
    return hs_client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=body
    ).to_dict()


def update_contact(hs_client, contact_id: str, props: dict) -> dict:
    """Update an existing HubSpot contact and return the updated object dict."""
    from hubspot.crm.contacts import SimplePublicObjectInput

    body = SimplePublicObjectInput(properties=props)
    return hs_client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=body,
    ).to_dict()


def add_note_to_contact(hs_client, contact_id: str, body: str) -> None:
    """Attach a plain-text note to a HubSpot contact."""
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate

    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    note = SimplePublicObjectInputForCreate(
        properties={
            "hs_note_body": body,
            "hs_timestamp": str(timestamp_ms),
        },
        associations=[{
            "to": {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": 202,  # Note → Contact
            }],
        }],
    )
    hs_client.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=note
    )


# ---------------------------------------------------------------------------
# State (processed message IDs)
# ---------------------------------------------------------------------------

def load_state() -> set[str]:
    path = Path(STATE_FILE)
    if path.exists():
        return set(json.loads(path.read_text()))
    return set()


def save_state(state: set[str]) -> None:
    Path(STATE_FILE).write_text(json.dumps(sorted(state), indent=2))


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_inbox(
    since_hours: int = LOOKBACK_HOURS,
    dry_run: bool = False,
    own_emails: Optional[set[str]] = None,
) -> list[dict]:
    """
    Sync Gmail inbox senders to HubSpot.
    Returns a list of result dicts with keys: status, email, contact_id, name.
    """
    gmail = get_gmail_service()
    hs_client = get_hubspot_client()
    label_id = None if dry_run else get_or_create_label(gmail, INBOUND_GMAIL_TAG)

    state = load_state()
    own_emails = {e.lower() for e in (own_emails or [])} | {GMAIL_ACCOUNT}

    raw_messages = fetch_inbox_messages(gmail, since_hours)
    log.info("Found %d inbox messages in the last %dh", len(raw_messages), since_hours)

    # Deduplicate: one pass per unique sender e-mail
    seen_senders: dict[str, list[str]] = {}  # email → [thread_ids]
    for m in raw_messages:
        if m["id"] in state:
            continue
        headers = get_message_headers(gmail, m["id"])
        raw_from = headers.get("From", "")
        display_name, email_address = parse_sender(raw_from)

        if not email_address or email_address in own_emails:
            state.add(m["id"])
            continue
        if should_skip(email_address, display_name):
            state.add(m["id"])
            continue

        seen_senders.setdefault(email_address, [])
        seen_senders[email_address].append(headers.get("threadId", ""))
        # Store display name on first encounter
        if "_display" not in seen_senders:
            seen_senders.setdefault(f"_display_{email_address}", display_name)  # type: ignore[arg-type]
        state.add(m["id"])

    results: list[dict] = []

    for email, thread_ids in seen_senders.items():
        if email.startswith("_display_"):
            continue

        display_name: str = seen_senders.get(f"_display_{email}", "")  # type: ignore[assignment]
        firstname, lastname = split_name(display_name)
        domain = domain_from_email(email)
        company = company_from_domain(domain)

        # If name is empty, derive first name from local part of email
        if not firstname:
            local = email.split("@")[0]
            # e.g. "stefania.dimitrio" → "Stefania Dimitrio"
            parts = re.split(r"[._\-+]", local)
            if len(parts) >= 2:
                firstname = parts[0].capitalize()
                lastname = " ".join(p.capitalize() for p in parts[1:])
            else:
                firstname = local.capitalize()

        log.info("Processing: %s <%s> | company=%s", display_name or firstname, email, company)

        if dry_run:
            results.append({
                "status": "DRY_RUN",
                "email": email,
                "contact_id": None,
                "name": f"{firstname} {lastname}".strip(),
                "company": company,
            })
            continue

        try:
            existing = search_contact_by_email(hs_client, email)
            props = build_contact_properties(email, firstname, lastname, company, existing=existing)

            if existing is None:
                if props:
                    created = create_contact(hs_client, props)
                    contact_id = created["id"]
                    add_note_to_contact(hs_client, contact_id, INBOUND_GMAIL_NOTE_BODY)
                    status = "Creato"
                else:
                    # Should not happen, but guard against empty props
                    status = "Ignorato (nessun dato)"
                    contact_id = None
            else:
                contact_id = existing["id"]
                if props:
                    update_contact(hs_client, contact_id, props)
                    status = "Aggiornato"
                else:
                    status = "Ignorato (già completo)"

            # Apply Gmail label to processed threads
            if label_id:
                for tid in set(thread_ids):
                    if tid:
                        try:
                            gmail.users().threads().modify(
                                userId="me",
                                id=tid,
                                body={"addLabelIds": [label_id]},
                            ).execute()
                        except Exception:
                            pass  # label failure is non-critical

            results.append({
                "status": status,
                "email": email,
                "contact_id": contact_id,
                "name": f"{firstname} {lastname}".strip(),
                "company": company,
            })

        except Exception as exc:
            log.error("Error processing %s: %s", email, exc)
            results.append({
                "status": f"Errore: {exc}",
                "email": email,
                "contact_id": None,
                "name": "",
                "company": company,
            })

    if not dry_run:
        save_state(state)

    return results


def print_results(results: list[dict]) -> None:
    if not results:
        print("\nNessuna nuova email da processare.\n")
        return

    print(f"\n{'─'*72}")
    print(f"  {'STATO':<20}  {'EMAIL':<36}  {'ID HUBSPOT':<14}  NOME")
    print(f"{'─'*72}")
    for r in results:
        cid = str(r.get("contact_id") or "—")
        name = r.get("name", "")
        company = r.get("company", "")
        label = f"{name} ({company})" if company else name
        print(f"  {r['status']:<20}  {r['email']:<36}  {cid:<14}  {label}")
    print(f"{'─'*72}\n")
    totals = {}
    for r in results:
        s = r["status"].split(":")[0]
        totals[s] = totals.get(s, 0) + 1
    print("Riepilogo: " + " | ".join(f"{v}x {k}" for k, v in totals.items()))
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--setup", action="store_true", help="Run Gmail OAuth setup flow and exit")
    p.add_argument("--hours", type=int, default=LOOKBACK_HOURS, help="Look back N hours (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing to HubSpot")
    p.add_argument("--watch", action="store_true", help="Keep running, polling every --interval seconds")
    p.add_argument("--interval", type=int, default=300, help="Polling interval in seconds (default: 300)")
    p.add_argument("--own-email", nargs="*", help="Own Gmail addresses to exclude from sync")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.setup:
        log.info("Starting Gmail OAuth flow...")
        get_gmail_service()
        log.info("Setup complete. Token saved to %s", TOKEN_FILE)
        return

    own_emails = set(args.own_email or [])

    if args.watch:
        log.info("Watching inbox every %ds. Press Ctrl+C to stop.", args.interval)
        while True:
            try:
                results = process_inbox(since_hours=args.hours, dry_run=args.dry_run, own_emails=own_emails)
                print_results(results)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log.error("Sync error: %s", exc)
            time.sleep(args.interval)
    else:
        results = process_inbox(since_hours=args.hours, dry_run=args.dry_run, own_emails=own_emails)
        print_results(results)


if __name__ == "__main__":
    main()
