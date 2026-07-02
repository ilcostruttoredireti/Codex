"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs sender contacts to HubSpot.
Avoids duplicates using email as unique key; updates existing records.

Usage:
    python gmail_hubspot_sync.py [--days N] [--dry-run]

Requires environment variables:
    GMAIL_CREDENTIALS_FILE   path to Gmail OAuth2 credentials.json
    GMAIL_TOKEN_FILE         path to store/load the OAuth token (default: token.json)
    HUBSPOT_API_KEY          HubSpot Private App token
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Optional

import requests

# ── optional Gmail API dependencies ──────────────────────────────────────────
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains and prefixes that are clearly automated — skip these entirely.
SKIP_PREFIXES = {
    "noreply", "no-reply", "mailer", "nobody", "system",
    "notifica", "invoicing", "autoresponder", "daemon",
    "postmaster", "bounce", "donotreply", "do-not-reply",
}
SKIP_DOMAINS = {
    "facebookmail.com", "twitter.com", "linkedin.com",
    "notifications.google.com",
}


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class Contact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source: str = "Gmail"

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""

    @property
    def company_from_domain(self) -> str:
        """Best-effort company name derived from the email domain."""
        domain = self.domain
        # strip common TLDs and sub-domains
        parts = domain.split(".")
        # remove trailing TLDs (last 1-2 parts)
        if len(parts) > 2:
            parts = parts[1:]          # drop leading subdomain (mail., updates., …)
        name = parts[0] if parts else domain
        return name.replace("-", " ").title()


@dataclass
class SyncResult:
    email: str
    status: str           # "Creato" | "Aggiornato" | "Ignorato"
    hubspot_id: Optional[str] = None
    reason: str = ""


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _gmail_creds(credentials_file: str, token_file: str) -> "Credentials":
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())
    return creds


def fetch_gmail_senders(days: int = 7, max_results: int = 200) -> list[Contact]:
    """Return a deduplicated list of Contacts from inbox messages in the last N days."""
    if not GMAIL_AVAILABLE:
        raise RuntimeError(
            "Google API libraries not installed. "
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )

    credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")

    creds = _gmail_creds(credentials_file, token_file)
    service = build("gmail", "v1", credentials=creds)

    query = f"in:inbox -from:me newer_than:{days}d"
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    messages = result.get("messages", [])
    log.info("Found %d messages matching query '%s'", len(messages), query)

    seen: dict[str, Contact] = {}
    for msg_meta in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_meta["id"], format="metadata",
                 metadataHeaders=["From", "Reply-To"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From") or headers.get("Reply-To", "")
        if not raw_from:
            continue

        display_name, email_addr = parseaddr(raw_from)
        email_addr = email_addr.strip().lower()
        if not email_addr or "@" not in email_addr:
            continue
        if email_addr in seen:
            continue
        if _should_skip(email_addr):
            log.debug("Skipping automated address: %s", email_addr)
            continue

        contact = _parse_contact(email_addr, display_name)
        seen[email_addr] = contact

    log.info("Extracted %d unique actionable senders", len(seen))
    return list(seen.values())


def _should_skip(email: str) -> bool:
    local, domain = email.split("@", 1)
    if domain in SKIP_DOMAINS:
        return True
    for prefix in SKIP_PREFIXES:
        if local == prefix or local.startswith(prefix + "-") or local.startswith(prefix + "+"):
            return True
    return False


def _parse_contact(email: str, display_name: str) -> Contact:
    c = Contact(email=email)
    name = display_name.strip()

    # Split "First Last" or "Last, First" into parts
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        c.lastname, c.firstname = parts[0], parts[1]
    else:
        parts = name.split()
        if len(parts) >= 2:
            c.firstname = parts[0]
            c.lastname = " ".join(parts[1:])
        elif len(parts) == 1:
            c.firstname = parts[0]

    # Derive company from domain when name is clearly not a person
    c.company = c.company_from_domain
    return c


# ── HubSpot helpers ───────────────────────────────────────────────────────────

HUBSPOT_BASE = "https://api.hubapi.com"


def _hs_headers() -> dict:
    token = os.environ.get("HUBSPOT_API_KEY", "")
    if not token:
        raise RuntimeError("HUBSPOT_API_KEY environment variable is not set.")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def hubspot_find_contact(email: str) -> Optional[dict]:
    """Return the first HubSpot contact matching the email, or None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hubspot_create_contact(contact: Contact) -> str:
    """Create a new HubSpot contact and return its id."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    props: dict = {
        "email": contact.email,
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if contact.firstname:
        props["firstname"] = contact.firstname
    if contact.lastname:
        props["lastname"] = contact.lastname
    if contact.company:
        props["company"] = contact.company

    resp = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]


def hubspot_update_contact(contact_id: str, updates: dict) -> None:
    """Patch a HubSpot contact with only the supplied fields."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": updates}, timeout=15)
    resp.raise_for_status()


# ── core sync logic ───────────────────────────────────────────────────────────

def sync_contact(contact: Contact, dry_run: bool = False) -> SyncResult:
    existing = hubspot_find_contact(contact.email)

    if existing is None:
        log.info("[CREATE] %s (%s)", contact.email, contact.company)
        if not dry_run:
            new_id = hubspot_create_contact(contact)
        else:
            new_id = "DRY-RUN"
        return SyncResult(email=contact.email, status="Creato", hubspot_id=new_id)

    hs_id = existing["id"]
    props = existing.get("properties", {})

    # Collect fields that are missing or empty in HubSpot
    updates: dict = {}
    if not props.get("firstname") and contact.firstname:
        updates["firstname"] = contact.firstname
    if not props.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname
    if not props.get("company") and contact.company:
        updates["company"] = contact.company
    if not props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    if updates:
        log.info("[UPDATE] %s (id=%s) fields=%s", contact.email, hs_id, list(updates))
        if not dry_run:
            hubspot_update_contact(hs_id, updates)
        return SyncResult(email=contact.email, status="Aggiornato", hubspot_id=hs_id,
                          reason=f"Aggiornati: {', '.join(updates)}")

    log.debug("[SKIP] %s already complete", contact.email)
    return SyncResult(email=contact.email, status="Ignorato", hubspot_id=hs_id,
                      reason="Nessun campo da aggiornare")


def run_sync(days: int = 7, dry_run: bool = False) -> list[SyncResult]:
    contacts = fetch_gmail_senders(days=days)
    results: list[SyncResult] = []
    for contact in contacts:
        try:
            result = sync_contact(contact, dry_run=dry_run)
            results.append(result)
        except requests.HTTPError as exc:
            log.error("HubSpot API error for %s: %s", contact.email, exc)
            results.append(SyncResult(email=contact.email, status="Errore", reason=str(exc)))
    return results


def print_report(results: list[SyncResult]) -> None:
    created  = [r for r in results if r.status == "Creato"]
    updated  = [r for r in results if r.status == "Aggiornato"]
    skipped  = [r for r in results if r.status == "Ignorato"]
    errors   = [r for r in results if r.status == "Errore"]

    print(f"\n{'─'*60}")
    print(f"  Gmail → HubSpot Sync — Riepilogo")
    print(f"{'─'*60}")
    print(f"  Creati:    {len(created)}")
    print(f"  Aggiornati:{len(updated)}")
    print(f"  Ignorati:  {len(skipped)}")
    print(f"  Errori:    {len(errors)}")
    print(f"{'─'*60}\n")

    for label, group in [("CREATO", created), ("AGGIORNATO", updated), ("ERRORE", errors)]:
        for r in group:
            print(f"  [{label}] {r.email:<40} ID: {r.hubspot_id or '—'}  {r.reason}")

    if skipped:
        print(f"\n  (+ {len(skipped)} contatti già completi ignorati)")
    print()


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts.")
    parser.add_argument("--days", type=int, default=7,
                        help="Scan emails from the last N days (default: 7)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Check and log without writing to HubSpot")
    args = parser.parse_args()

    log.info("Starting Gmail → HubSpot sync (days=%d, dry_run=%s)", args.days, args.dry_run)
    results = run_sync(days=args.days, dry_run=args.dry_run)
    print_report(results)
    log.info("Sync complete. %d contacts processed.", len(results))


if __name__ == "__main__":
    main()
