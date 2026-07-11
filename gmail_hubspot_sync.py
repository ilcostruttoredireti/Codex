#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Scans the Gmail inbox for new incoming emails, extracts sender info,
and upserts contacts into HubSpot:
  - Existing contact → update any missing fields
  - New contact     → create with email, name, company, source="Gmail"
  - Automated/no-reply senders → skip

Usage:
    export GMAIL_CREDENTIALS_PATH=credentials.json
    export HUBSPOT_ACCESS_TOKEN=pat-na1-xxxx
    python gmail_hubspot_sync.py [--query "in:inbox newer_than:7d"] [--dry-run]

Required packages:
    pip install -r requirements.txt
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skip rules – automated / system senders
# ---------------------------------------------------------------------------

_SKIP_LOCAL_PARTS = frozenset(
    {
        "noreply",
        "no-reply",
        "nobody",
        "donotreply",
        "do-not-reply",
        "bounce",
        "mailer-daemon",
        "postmaster",
        "auto-reply",
        "notifications",
        "dailybriefing",
        "messaging-digest-noreply",
    }
)

_SKIP_DOMAINS = frozenset(
    {
        "youtube.com",
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "googlealerts.com",
        "accounts.google.com",
        "mail.google.com",
    }
)

_GENERIC_LOCAL_PARTS = frozenset(
    {"info", "support", "staff", "commerciale", "formazione", "ciao", "product", "hello"}
)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    SKIPPED = "Saltato"


@dataclass
class ContactInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    lead_source: str = "Gmail"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    note: str = ""


# ---------------------------------------------------------------------------
# Sender parsing
# ---------------------------------------------------------------------------

def _split_name(display: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last)."""
    display = display.strip().strip('"').strip("'")
    parts = display.split(None, 1)
    if len(parts) == 2:
        return parts[0].capitalize(), parts[1].strip()
    if len(parts) == 1 and parts[0]:
        return parts[0].capitalize(), ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """
    Derive a readable company name from an email domain.
    'martes-ai.com' → 'Martes Ai', 'raffaprivatejet.com' → 'Raffaprivatejet'
    """
    base = domain.rsplit(".", 1)[0]  # strip TLD
    # strip known subdomain prefixes (e.g. 'c.recharge.com' → 'recharge')
    parts = base.split(".")
    base = parts[-1]
    return re.sub(r"[-_]", " ", base).title()


def parse_sender(raw_from: str) -> Optional[ContactInfo]:
    """
    Parse a Gmail 'From' header value into a ContactInfo.
    Returns None if the sender should be skipped.

    Handles:
      'Display Name <email@domain.com>'
      'email@domain.com'
    """
    m = re.match(r'"?([^<"]*)"?\s*<([^>]+)>', raw_from)
    if m:
        display, email = m.group(1).strip(), m.group(2).strip().lower()
    else:
        display, email = "", raw_from.strip().lower()

    if "@" not in email:
        return None

    local, domain = email.rsplit("@", 1)

    # Hard skip rules
    if local in _SKIP_LOCAL_PARTS:
        return None
    if domain in _SKIP_DOMAINS:
        return None
    # Catch 'noreply@…' or 'no-reply@…' as prefix
    if local.startswith("noreply") or local.startswith("no-reply") or local.startswith("nobody"):
        return None

    # Derive name
    firstname, lastname = ("", "")
    if display and display.lower() not in _GENERIC_LOCAL_PARTS:
        firstname, lastname = _split_name(display)
    elif local not in _GENERIC_LOCAL_PARTS:
        # Use local part: 'riccardo' → 'Riccardo', 'chelsea.c' → 'Chelsea C'
        parts = re.split(r"[._\-]", local)
        firstname = parts[0].capitalize()
        lastname = " ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else ""

    company = _company_from_domain(domain)

    return ContactInfo(
        email=email,
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
    )


# ---------------------------------------------------------------------------
# Gmail API helpers
# ---------------------------------------------------------------------------

def _build_gmail_service(credentials_path: str):
    """Build an authenticated Gmail API service."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(
        credentials_path,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    return build("gmail", "v1", credentials=creds)


def fetch_senders(service, query: str = "in:inbox newer_than:7d -from:me", max_results: int = 200) -> list[str]:
    """
    Fetch the raw 'From' header from each message matching *query*.
    Returns deduplicated list of sender strings.
    """
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    messages = result.get("messages", [])
    log.info("Gmail query returned %d messages", len(messages))

    senders: list[str] = []
    for msg in messages:
        detail = (
            service.users()
            .messages()
            .get(userId="me", id=msg["id"], format="metadata", metadataHeaders=["From"])
            .execute()
        )
        for header in detail.get("payload", {}).get("headers", []):
            if header["name"] == "From":
                senders.append(header["value"])
    return senders


# ---------------------------------------------------------------------------
# HubSpot API helpers
# ---------------------------------------------------------------------------

def _build_hubspot_client(access_token: str):
    """Return an authenticated HubSpot client."""
    import hubspot

    return hubspot.Client.create(access_token=access_token)


def _find_contact(client, email: str) -> Optional[object]:
    """Search HubSpot for a contact by email. Returns the contact or None."""
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "lead_source"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(req)
    return resp.results[0] if resp.results else None


def _missing_fields(contact, info: ContactInfo) -> dict:
    """Return HubSpot property dict with only the fields missing on *contact*."""
    p = contact.properties
    updates: dict[str, str] = {}
    if not p.get("firstname") and info.firstname:
        updates["firstname"] = info.firstname
    if not p.get("lastname") and info.lastname:
        updates["lastname"] = info.lastname
    if not p.get("company") and info.company:
        updates["company"] = info.company
    if not p.get("lead_source"):
        updates["lead_source"] = info.lead_source
    return updates


def upsert_contact(client, info: ContactInfo, dry_run: bool = False) -> SyncResult:
    """
    Create or update a HubSpot contact for *info*.
    In dry-run mode, no writes are performed.
    """
    from hubspot.crm.contacts import ApiException

    try:
        existing = _find_contact(client, info.email)
        if existing:
            updates = _missing_fields(existing, info)
            if not updates:
                return SyncResult(SyncStatus.IGNORED, info.email, hubspot_id=str(existing.id))
            if not dry_run:
                client.crm.contacts.basic_api.update(
                    contact_id=existing.id,
                    simple_public_object_input={"properties": updates},
                )
            return SyncResult(SyncStatus.UPDATED, info.email, hubspot_id=str(existing.id), note=f"updated: {list(updates.keys())}")
        else:
            props = {
                "email": info.email,
                "firstname": info.firstname,
                "lastname": info.lastname,
                "company": info.company,
                "lead_source": info.lead_source,
            }
            if not dry_run:
                resp = client.crm.contacts.basic_api.create(
                    simple_public_object_input_for_create={"properties": props}
                )
                return SyncResult(SyncStatus.CREATED, info.email, hubspot_id=str(resp.id))
            return SyncResult(SyncStatus.CREATED, info.email, hubspot_id="[dry-run]")
    except ApiException as exc:
        log.error("HubSpot error for %s: %s", info.email, exc)
        return SyncResult(SyncStatus.SKIPPED, info.email, note=str(exc))


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------

def run_sync(
    gmail_credentials: str,
    hubspot_token: str,
    query: str = "in:inbox newer_than:7d -from:me",
    dry_run: bool = False,
) -> list[SyncResult]:
    """Main entry point: fetch Gmail senders and upsert into HubSpot."""
    log.info("Starting Gmail → HubSpot sync (dry_run=%s)", dry_run)

    gmail = _build_gmail_service(gmail_credentials)
    hs = _build_hubspot_client(hubspot_token)

    raw_senders = fetch_senders(gmail, query=query)
    log.info("Parsing %d raw sender headers", len(raw_senders))

    seen: set[str] = set()
    results: list[SyncResult] = []

    for raw in raw_senders:
        contact = parse_sender(raw)
        if contact is None:
            log.debug("Skipped (automated): %s", raw)
            continue
        if contact.email in seen:
            continue
        seen.add(contact.email)
        log.info("→ %s (%s)", contact.email, contact.company)
        result = upsert_contact(hs, contact, dry_run=dry_run)
        log.info("   %s  id=%s  %s", result.status, result.hubspot_id, result.note)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# CLI report
# ---------------------------------------------------------------------------

def print_report(results: list[SyncResult]) -> None:
    line = "=" * 70
    print(f"\n{line}")
    print(f"{'STATO':<12} {'EMAIL':<38} {'HUBSPOT ID':<16} NOTE")
    print(line)
    for r in results:
        print(f"{r.status:<12} {r.email:<38} {(r.hubspot_id or '-'):<16} {r.note}")
    print(line)
    counts = {s: sum(1 for r in results if r.status == s) for s in SyncStatus}
    print(
        f"Totale: {len(results)}  |  "
        f"Creati: {counts[SyncStatus.CREATED]}  |  "
        f"Aggiornati: {counts[SyncStatus.UPDATED]}  |  "
        f"Ignorati: {counts[SyncStatus.IGNORED]}  |  "
        f"Saltati: {counts[SyncStatus.SKIPPED]}"
    )
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--gmail-credentials",
        default=os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"),
        help="Path to Gmail OAuth2 credentials JSON file",
    )
    parser.add_argument(
        "--hubspot-token",
        default=os.getenv("HUBSPOT_ACCESS_TOKEN", ""),
        help="HubSpot Private App access token",
    )
    parser.add_argument(
        "--query",
        default="in:inbox newer_than:7d -from:me",
        help="Gmail search query to select messages",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and check contacts but do not write to HubSpot",
    )
    parser.add_argument(
        "--output-json",
        metavar="FILE",
        help="Write results as JSON to FILE",
    )
    args = parser.parse_args()

    if not args.hubspot_token:
        log.error("HUBSPOT_ACCESS_TOKEN not set. Pass --hubspot-token or set the env var.")
        sys.exit(1)

    results = run_sync(
        gmail_credentials=args.gmail_credentials,
        hubspot_token=args.hubspot_token,
        query=args.query,
        dry_run=args.dry_run,
    )

    print_report(results)

    if args.output_json:
        with open(args.output_json, "w") as fh:
            json.dump(
                [{"status": r.status, "email": r.email, "hubspot_id": r.hubspot_id, "note": r.note} for r in results],
                fh,
                indent=2,
                ensure_ascii=False,
            )
        log.info("Results written to %s", args.output_json)
