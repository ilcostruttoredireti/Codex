#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Monitors incoming Gmail emails, extracts sender contact data, and upserts
records into HubSpot CRM — creating new contacts or filling missing fields
on existing ones.

Usage:
    python gmail_hubspot_sync.py [--query "..."] [--page-size N] [--dry-run]

Requirements:
    pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client

Environment variables (or OAuth credentials):
    HUBSPOT_ACCESS_TOKEN   – HubSpot private-app token
    GOOGLE_CREDENTIALS     – path to credentials.json for Gmail OAuth
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

STATE_FILE = Path("sync_state.json")

# Automated / infrastructure senders to ignore
SKIP_DOMAINS: set[str] = {
    "facebookmail.com",
    "bounce.em.facebook.com",
    "accounts.google.com",
    "mailer.facebook.com",
    "amazonses.com",
    "sendgrid.net",
    "mailchimp.com",
    "mandrillapp.com",
}

# Free mailbox domains — don't infer company from these
GENERIC_DOMAINS: set[str] = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "me.com", "protonmail.com",
}

# Regex to detect original sender in Italian-style forwarded emails
# Pattern: Da "Name" email@domain   or   Da email@domain
_FWD_PATTERN = re.compile(
    r'Da\s+(?:"([^"]+)"\s+)?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
    re.IGNORECASE,
)


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Contact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""
    source_thread: str = ""
    subject: str = ""


@dataclass
class SyncResult:
    status: str          # Creato | Aggiornato | Ignorato
    email: str
    hubspot_id: Optional[str] = None
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "email": self.email,
            "hubspot_id": self.hubspot_id,
            "detail": self.detail,
        }


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _extract_domain(email: str) -> str:
    return email.split("@")[-1].lower() if "@" in email else ""


def _domain_to_company(domain: str) -> str:
    """Best-effort company name from a domain (e.g. altabadia.org → Alta Badia)."""
    parts = domain.split(".")
    # Drop common TLD segments (co, gov, edu, org, com, it, ch, …)
    name_part = parts[-2] if len(parts) >= 2 else parts[0]
    return name_part.replace("-", " ").title()


def _parse_display_name(display: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last), handling multi-word first names."""
    display = display.strip().strip('"')
    tokens = display.split()
    if not tokens:
        return "", ""
    if len(tokens) == 1:
        return tokens[0].title(), ""
    return " ".join(tokens[:-1]).title(), tokens[-1].title()


def _parse_sender(raw: str, skip_emails: set[str] | None = None) -> Optional[Contact]:
    """Parse a raw 'From' header value into a Contact, or return None to skip."""
    display, email = parseaddr(raw)
    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    if skip_emails and email in skip_emails:
        return None

    domain = _extract_domain(email)
    if domain in SKIP_DOMAINS:
        return None

    first, last = _parse_display_name(display) if display else ("", "")
    company = "" if domain in GENERIC_DOMAINS else _domain_to_company(domain)

    return Contact(email=email, first_name=first, last_name=last,
                   company=company, domain=domain)


def _extract_forwarded_senders(snippet: str) -> list[Contact]:
    """
    Extract original sender contacts from the text body of a forwarded email.
    Handles the Italian forwarding format:  Da "Name" email@domain
    """
    contacts: list[Contact] = []
    for m in _FWD_PATTERN.finditer(snippet):
        display_name = m.group(1) or ""
        email_addr = m.group(2).lower().strip()
        domain = _extract_domain(email_addr)
        if domain in SKIP_DOMAINS or domain in GENERIC_DOMAINS:
            continue
        first, last = _parse_display_name(display_name) if display_name else ("", "")
        company = _domain_to_company(domain)
        contacts.append(Contact(email=email_addr, first_name=first, last_name=last,
                                company=company, domain=domain))
    return contacts


# ── Gmail client wrapper ───────────────────────────────────────────────────────

class GmailClient:
    """Thin wrapper around the Gmail API v1."""

    def __init__(self):
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
        creds_path = os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")
        token_path = "token.json"

        creds = None
        if Path(token_path).exists():
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
            Path(token_path).write_text(creds.to_json())

        self._svc = build("gmail", "v1", credentials=creds)

    def search_threads(self, query: str, max_results: int = 50) -> list[dict]:
        result = self._svc.users().threads().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        threads = []
        for t in result.get("threads", []):
            full = self._svc.users().threads().get(
                userId="me", id=t["id"], format="metadata",
                metadataHeaders=["From", "Subject"]
            ).execute()
            threads.append(full)
        return threads

    def get_last_message_timestamp(self, thread: dict) -> int:
        msgs = thread.get("messages", [])
        return int(msgs[-1]["internalDate"]) if msgs else 0


# ── HubSpot client wrapper ─────────────────────────────────────────────────────

class HubSpotClient:
    """Thin wrapper around the HubSpot CRM v3 contacts API."""

    def __init__(self):
        import hubspot
        from hubspot.crm.contacts import ApiException

        token = os.environ["HUBSPOT_ACCESS_TOKEN"]
        self._client = hubspot.Client.create(access_token=token)
        self._ApiException = ApiException

    def find_by_email(self, email: str) -> Optional[dict]:
        from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

        f = Filter(property_name="email", operator="EQ", value=email)
        fg = FilterGroup(filters=[f])
        req = PublicObjectSearchRequest(
            filter_groups=[fg],
            properties=["email", "firstname", "lastname", "company", "leadsource"],
        )
        try:
            res = self._client.crm.contacts.search_api.do_search(req)
            return res.results[0].to_dict() if res.results else None
        except self._ApiException:
            return None

    def create(self, props: dict) -> dict:
        from hubspot.crm.contacts.models import SimplePublicObjectInputForCreate
        obj = SimplePublicObjectInputForCreate(properties=props)
        result = self._client.crm.contacts.basic_api.create(obj)
        return result.to_dict()

    def update(self, contact_id: str, props: dict) -> None:
        from hubspot.crm.contacts.models import SimplePublicObjectInput
        obj = SimplePublicObjectInput(properties=props)
        self._client.crm.contacts.basic_api.update(contact_id, obj)


# ── Sync engine ────────────────────────────────────────────────────────────────

# HubSpot property values for Gmail-sourced contacts.
# hs_analytics_source is an enum; "OFFLINE" is the correct bucket for contacts
# acquired via direct email/manual channels (not website traffic).
# Note: hs_analytics_source_data_1 / _data_2 are read-only in HubSpot and
# cannot be set via the API — they are only populated by the tracking code.
HS_ANALYTICS_SOURCE = "OFFLINE"


class GmailHubSpotSync:
    """Orchestrates the Gmail → HubSpot sync pass."""

    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient, dry_run: bool = False):
        self.gmail = gmail
        self.hubspot = hubspot
        self.dry_run = dry_run

    # ── public API ────────────────────────────────────────────────────────────

    def run(
        self,
        query: str = "in:inbox newer_than:1d -from:me",
        page_size: int = 50,
        own_email: str = "",
    ) -> list[SyncResult]:
        log.info("Starting sync | query=%r dry_run=%s", query, self.dry_run)
        threads = self.gmail.search_threads(query=query, max_results=page_size)
        log.info("Fetched %d threads", len(threads))

        skip_emails: set[str] = {own_email.lower()} if own_email else set()
        seen: set[str] = set()
        results: list[SyncResult] = []

        for thread in threads:
            for msg in thread.get("messages", []):
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                from_hdr = headers.get("From", "")
                subject = headers.get("Subject", "")

                # Primary sender
                contact = _parse_sender(from_hdr, skip_emails)
                if contact:
                    contact.source_thread = thread["id"]
                    contact.subject = subject
                    if contact.email not in seen:
                        seen.add(contact.email)
                        results.append(self._upsert(contact))

                # Also mine forwarded-email body for the original sender
                for fwd_contact in _extract_forwarded_senders(
                    msg.get("snippet", "") + " " + _get_body_text(msg)
                ):
                    fwd_contact.source_thread = thread["id"]
                    fwd_contact.subject = subject
                    if fwd_contact.email not in seen:
                        seen.add(fwd_contact.email)
                        results.append(self._upsert(fwd_contact))

        self._print_summary(results)
        return results

    # ── internal helpers ──────────────────────────────────────────────────────

    def _upsert(self, contact: Contact) -> SyncResult:
        existing = self.hubspot.find_by_email(contact.email)
        props = self._build_props(contact)

        if existing:
            hs_id = existing["id"]
            current_props = existing.get("properties", {})
            update = {k: v for k, v in props.items()
                      if k != "email" and v and not current_props.get(k)}

            if not update:
                return SyncResult("Ignorato", contact.email, hs_id,
                                  "tutti i campi già presenti")

            if not self.dry_run:
                self.hubspot.update(hs_id, update)
            return SyncResult("Aggiornato", contact.email, hs_id,
                              f"campi aggiornati: {', '.join(update)}")
        else:
            if not self.dry_run:
                created = self.hubspot.create(props)
                hs_id = created["id"]
            else:
                hs_id = "DRY_RUN"
            return SyncResult("Creato", contact.email, hs_id)

    @staticmethod
    def _build_props(contact: Contact) -> dict:
        props: dict[str, str] = {
            "email": contact.email,
            "hs_analytics_source": HS_ANALYTICS_SOURCE,
        }
        if contact.first_name:
            props["firstname"] = contact.first_name
        if contact.last_name:
            props["lastname"] = contact.last_name
        if contact.company:
            props["company"] = contact.company
        return props

    @staticmethod
    def _print_summary(results: list[SyncResult]) -> None:
        created = [r for r in results if r.status == "Creato"]
        updated = [r for r in results if r.status == "Aggiornato"]
        skipped = [r for r in results if r.status == "Ignorato"]
        print(f"\n{'='*60}")
        print(f"Sync completato: {len(results)} contatti processati")
        print(f"  Creati:    {len(created)}")
        print(f"  Aggiornati:{len(updated)}")
        print(f"  Ignorati:  {len(skipped)}")
        print(f"{'='*60}")
        print(f"\n{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
        print("-" * 80)
        for r in results:
            print(f"{r.status:<12} {r.email:<45} {r.hubspot_id or '—'}")


# ── Body extraction helper ─────────────────────────────────────────────────────

def _get_body_text(message: dict) -> str:
    """Recursively collect plain-text body parts from a Gmail message payload."""
    payload = message.get("payload", {})

    def _recurse(part: dict) -> str:
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            import base64
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
        for sub in part.get("parts", []):
            text = _recurse(sub)
            if text:
                return text
        return ""

    return _recurse(payload)


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run_utc": None, "processed_count": 0}


def save_state(state: dict) -> None:
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── CLI entry point ────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument("--query", default="in:inbox newer_than:1d -from:me",
                        help="Gmail search query (default: inbox, last 24 h, excluding own sent)")
    parser.add_argument("--page-size", type=int, default=50,
                        help="Max threads to fetch per run (default: 50)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and log without writing to HubSpot")
    args = parser.parse_args()

    state = load_state()
    log.info("Last run: %s | total processed so far: %d",
             state.get("last_run_utc", "never"), state.get("processed_count", 0))

    gmail = GmailClient()
    hubspot = HubSpotClient()
    sync = GmailHubSpotSync(gmail, hubspot, dry_run=args.dry_run)

    results = sync.run(
        query=args.query,
        page_size=args.page_size,
        own_email=os.environ.get("GMAIL_OWN_EMAIL", ""),
    )

    if not args.dry_run:
        state["processed_count"] = state.get("processed_count", 0) + len(results)
        save_state(state)

    sys.exit(0)


if __name__ == "__main__":
    main()
