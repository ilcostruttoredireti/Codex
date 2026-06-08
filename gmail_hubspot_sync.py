#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs sender contacts to HubSpot.

Usage:
    python gmail_hubspot_sync.py              # continuous monitoring
    python gmail_hubspot_sync.py --once       # single pass, then exit
    python gmail_hubspot_sync.py --since 24h  # process emails from last N hours
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

# ── third-party ──────────────────────────────────────────────────────────────
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    sys.exit("Missing Google client libs. Run: pip install -r requirements.txt")

try:
    import hubspot
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
    from hubspot.crm.contacts.exceptions import ApiException
except ImportError:
    sys.exit("Missing HubSpot client lib. Run: pip install -r requirements.txt")

# ── constants ─────────────────────────────────────────────────────────────────
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
TOKEN_FILE = Path("gmail_token.json")
CREDENTIALS_FILE = Path("credentials.json")
STATE_FILE = Path("processed_emails.json")
POLL_INTERVAL_SEC = 60

# Free/generic email providers — company name cannot be derived from domain
FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.fr",
    "yahoo.co.uk", "hotmail.com", "hotmail.it", "outlook.com", "live.com",
    "live.it", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "tutanota.com", "fastmail.com",
    "libero.it", "virgilio.it", "tin.it", "alice.it", "tiscali.it",
    "email.it", "iol.it", "katamail.com",
}

NOREPLY_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "daemon", "auto-reply",
    "notification", "notifications", "alert", "alerts",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── data classes ──────────────────────────────────────────────────────────────
@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""

    @classmethod
    def from_header(cls, from_header: str) -> Optional["SenderContact"]:
        """Parse a From: header into a SenderContact. Returns None for no-reply."""
        display_name, address = parseaddr(from_header)
        address = address.strip().lower()
        if not address or "@" not in address:
            return None

        local, domain = address.split("@", 1)

        # Skip no-reply / automated senders
        if local.startswith(NOREPLY_PREFIXES):
            return None

        first, last = _split_name(display_name)
        company = _company_from_domain(domain)

        return cls(
            email=address,
            first_name=first,
            last_name=last,
            company=company,
            domain=domain,
        )


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored"
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""


# ── helpers ───────────────────────────────────────────────────────────────────
def _split_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (first, last)."""
    name = display_name.strip().strip('"').strip("'")
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """Derive a company name from an email domain, or '' for free providers."""
    root = domain.lower()
    # strip common subdomains (mail.company.com → company.com)
    parts = root.split(".")
    if len(parts) > 2:
        root = ".".join(parts[-2:])
    if root in FREE_PROVIDERS:
        return ""
    company = parts[-2] if len(parts) >= 2 else root
    return company.replace("-", " ").replace("_", " ").title()


def _parse_since(since_str: str) -> int:
    """Parse '24h', '7d', '30m' → Unix timestamp."""
    match = re.fullmatch(r"(\d+)([hHdDmM])", since_str)
    if not match:
        raise ValueError(f"Invalid --since value: {since_str}. Use e.g. 24h, 7d, 30m.")
    value, unit = int(match.group(1)), match.group(2).lower()
    delta = {"h": timedelta(hours=value), "d": timedelta(days=value), "m": timedelta(minutes=value)}[unit]
    return int((datetime.now(timezone.utc) - delta).timestamp())


# ── state management ──────────────────────────────────────────────────────────
class ProcessedState:
    """Persists the set of already-processed Gmail message IDs."""

    def __init__(self, path: Path = STATE_FILE):
        self._path = path
        self._ids: set[str] = set()
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._ids = set(data.get("processed_ids", []))
                log.debug("Loaded %d processed IDs from state file.", len(self._ids))
            except (json.JSONDecodeError, KeyError):
                log.warning("State file corrupt — starting fresh.")

    def save(self):
        self._path.write_text(
            json.dumps({"processed_ids": list(self._ids)}, indent=2)
        )

    def is_processed(self, msg_id: str) -> bool:
        return msg_id in self._ids

    def mark(self, msg_id: str):
        self._ids.add(msg_id)


# ── Gmail client ──────────────────────────────────────────────────────────────
class GmailClient:
    def __init__(self):
        self._service = self._build_service()

    def _build_service(self):
        creds = None
        if TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not CREDENTIALS_FILE.exists():
                    sys.exit(
                        f"OAuth credentials file not found: {CREDENTIALS_FILE}\n"
                        "Download it from Google Cloud Console and place it here."
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
                creds = flow.run_local_server(port=0)
            TOKEN_FILE.write_text(creds.to_json())

        return build("gmail", "v1", credentials=creds)

    def fetch_inbox_messages(self, after_timestamp: Optional[int] = None) -> list[dict]:
        """Return all INBOX messages (excluding drafts/sent) since *after_timestamp*."""
        query_parts = ["in:inbox", "-in:sent", "-in:draft"]
        if after_timestamp:
            query_parts.append(f"after:{after_timestamp}")
        query = " ".join(query_parts)

        messages = []
        page_token = None
        while True:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 100}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = self._service.users().messages().list(**kwargs).execute()
            messages.extend(resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return messages

    def get_message_headers(self, msg_id: str) -> dict[str, str]:
        """Fetch only the headers we need (lightweight format=metadata)."""
        msg = (
            self._service.users()
            .messages()
            .get(userId="me", id=msg_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        headers["_id"] = msg_id
        headers["_snippet"] = msg.get("snippet", "")
        return headers


# ── HubSpot client ────────────────────────────────────────────────────────────
class HubSpotClient:
    def __init__(self, api_key: str):
        self._client = hubspot.Client.create(access_token=api_key)

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return existing HubSpot contact dict or None."""
        try:
            from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup
            f = Filter(property_name="email", operator="EQ", value=email)
            fg = FilterGroup(filters=[f])
            req = PublicObjectSearchRequest(
                filter_groups=[fg],
                properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
                limit=1,
            )
            resp = self._client.crm.contacts.search_api.do_search(public_object_search_request=req)
            if resp.results:
                return {"id": resp.results[0].id, "properties": resp.results[0].properties}
        except ApiException as exc:
            log.error("HubSpot search error: %s", exc)
        return None

    def create_contact(self, contact: SenderContact) -> Optional[str]:
        """Create a new HubSpot contact. Returns the new contact ID."""
        props = self._build_properties(contact)
        try:
            obj = SimplePublicObjectInputForCreate(properties=props)
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=obj
            )
            return result.id
        except ApiException as exc:
            log.error("HubSpot create error for %s: %s", contact.email, exc)
            return None

    def update_contact(self, contact_id: str, contact: SenderContact, existing: dict) -> bool:
        """Update only missing fields on an existing contact. Returns True if any field changed."""
        existing_props = existing.get("properties", {})
        updates: dict[str, str] = {}

        def _needs(key: str, new_val: str) -> bool:
            return bool(new_val) and not existing_props.get(key)

        if _needs("firstname", contact.first_name):
            updates["firstname"] = contact.first_name
        if _needs("lastname", contact.last_name):
            updates["lastname"] = contact.last_name
        if _needs("company", contact.company):
            updates["company"] = contact.company
        # Always ensure source tag
        if existing_props.get("hs_lead_source") != "GMAIL":
            updates["hs_lead_source"] = "GMAIL"

        if not updates:
            return False

        try:
            obj = SimplePublicObjectInput(properties=updates)
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=obj,
            )
            return True
        except ApiException as exc:
            log.error("HubSpot update error for contact %s: %s", contact_id, exc)
            return False

    def _build_properties(self, contact: SenderContact) -> dict[str, str]:
        props: dict[str, str] = {"email": contact.email, "hs_lead_source": "GMAIL"}
        if contact.first_name:
            props["firstname"] = contact.first_name
        if contact.last_name:
            props["lastname"] = contact.last_name
        if contact.company:
            props["company"] = contact.company
        return props


# ── sync engine ───────────────────────────────────────────────────────────────
class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot_client: HubSpotClient, state: ProcessedState):
        self._gmail = gmail
        self._hs = hubspot_client
        self._state = state

    def process_batch(self, after_timestamp: Optional[int] = None) -> list[SyncResult]:
        messages = self._gmail.fetch_inbox_messages(after_timestamp)
        log.info("Found %d message(s) to inspect.", len(messages))

        results: list[SyncResult] = []
        for msg_ref in messages:
            msg_id = msg_ref["id"]
            if self._state.is_processed(msg_id):
                continue
            result = self._process_message(msg_id)
            results.append(result)
            self._state.mark(msg_id)

        self._state.save()
        return results

    def _process_message(self, msg_id: str) -> SyncResult:
        try:
            headers = self._gmail.get_message_headers(msg_id)
        except HttpError as exc:
            log.error("Gmail fetch error for %s: %s", msg_id, exc)
            return SyncResult(status="ignored", email="?", reason="gmail_error")

        from_header = headers.get("From", "")
        contact = SenderContact.from_header(from_header)

        if not contact:
            log.debug("Skipping message %s — no-reply or unparseable From header.", msg_id)
            return SyncResult(status="ignored", email=from_header or "?", reason="noreply_or_invalid")

        existing = self._hs.find_contact_by_email(contact.email)

        if existing:
            changed = self._hs.update_contact(existing["id"], contact, existing)
            status = "updated" if changed else "ignored"
            reason = "fields_updated" if changed else "already_complete"
            log.info("[%s] %s — HubSpot ID %s", status.upper(), contact.email, existing["id"])
            return SyncResult(status=status, email=contact.email, hubspot_id=existing["id"], reason=reason)
        else:
            new_id = self._hs.create_contact(contact)
            if new_id:
                log.info("[CREATED] %s — HubSpot ID %s", contact.email, new_id)
                return SyncResult(status="created", email=contact.email, hubspot_id=new_id)
            return SyncResult(status="ignored", email=contact.email, reason="hubspot_create_failed")


# ── reporting ─────────────────────────────────────────────────────────────────
def print_report(results: list[SyncResult]):
    if not results:
        print("  (no new messages processed)")
        return

    width = 60
    print(f"\n{'─' * width}")
    print(f"  {'STATO':<12} {'EMAIL':<35} {'HUBSPOT ID'}")
    print(f"{'─' * width}")
    for r in results:
        status_icon = {"created": "✅ Creato", "updated": "🔄 Aggiornato", "ignored": "⏭  Ignorato"}
        icon = status_icon.get(r.status, r.status)
        hs_id = r.hubspot_id or "-"
        print(f"  {icon:<14} {r.email:<35} {hs_id}")
    print(f"{'─' * width}")
    created = sum(1 for r in results if r.status == "created")
    updated = sum(1 for r in results if r.status == "updated")
    ignored = sum(1 for r in results if r.status == "ignored")
    print(f"  Totale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {ignored}\n")


# ── entrypoint ────────────────────────────────────────────────────────────────
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sincronizza contatti da Gmail a HubSpot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--once", action="store_true", help="Esegui un solo ciclo e termina.")
    p.add_argument(
        "--since",
        default="24h",
        metavar="DURATION",
        help="Processa le email degli ultimi N ore/giorni/minuti (es. 24h, 7d, 30m). Default: 24h",
    )
    p.add_argument("--interval", type=int, default=POLL_INTERVAL_SEC,
                   help=f"Secondi tra un ciclo e l'altro (default: {POLL_INTERVAL_SEC}).")
    p.add_argument("--debug", action="store_true", help="Abilita log di debug.")
    return p


def main():
    args = build_arg_parser().parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    hubspot_key = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not hubspot_key:
        sys.exit(
            "Missing HUBSPOT_ACCESS_TOKEN environment variable.\n"
            "Set it with: export HUBSPOT_ACCESS_TOKEN=<your_private_app_token>"
        )

    log.info("Inizializzazione Gmail client…")
    gmail = GmailClient()
    log.info("Inizializzazione HubSpot client…")
    hs_client = HubSpotClient(hubspot_key)
    state = ProcessedState()
    engine = SyncEngine(gmail, hs_client, state)

    after_ts = _parse_since(args.since)
    log.info("Monitoraggio Gmail → HubSpot avviato (since=%s, interval=%ds).", args.since, args.interval)

    try:
        while True:
            log.info("--- Ciclo di sync: %s ---", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            results = engine.process_batch(after_timestamp=after_ts)
            print_report(results)

            if args.once:
                break

            # On subsequent loops we only look at messages since the last run
            after_ts = int(datetime.now(timezone.utc).timestamp()) - args.interval
            log.info("Prossimo ciclo tra %d secondi…", args.interval)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Interruzione manuale. Stato salvato.")
        state.save()


if __name__ == "__main__":
    main()
