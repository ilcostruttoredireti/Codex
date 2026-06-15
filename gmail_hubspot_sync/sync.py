"""
Gmail → HubSpot Contact Sync
-----------------------------
Reads incoming Gmail threads (last N days), extracts real senders (including
from forwarded emails), then creates or updates HubSpot contacts, tagging them
as "Inbound Gmail" and setting the lead source to "Gmail".

Dependencies:
    pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib hubspot-api-client

Environment variables required:
    HUBSPOT_ACCESS_TOKEN   – HubSpot private-app token with contacts read/write scope
    GOOGLE_CREDENTIALS     – path to Google OAuth2 credentials JSON file
    GOOGLE_TOKEN           – path to cached token JSON file (created on first run)
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Sender:
    email_addr: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    subject: str = ""

    @property
    def domain(self) -> str:
        return self.email_addr.split("@")[-1] if "@" in self.email_addr else ""

    def company_from_domain(self) -> str:
        """Best-effort company name from the sender's domain."""
        if self.company:
            return self.company
        domain = self.domain
        # Skip generic providers
        generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                   "icloud.com", "virgilio.it", "libero.it", "tiscali.it"}
        if domain in generic:
            return ""
        # Strip TLD(s) and capitalise
        parts = domain.split(".")
        name = parts[0] if parts else domain
        return name.replace("-", " ").replace("_", " ").title()


@dataclass
class SyncResult:
    email_addr: str
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    contact_id: str = ""
    note: str = ""


# ---------------------------------------------------------------------------
# Helpers: parse forwarded-email headers from snippets / full body
# ---------------------------------------------------------------------------

# Common Italian/English forwarding header patterns
_FWD_FROM_PATTERNS = [
    # Da "Nome Cognome" email@domain.com
    r'Da\s+"([^"]+)"\s+([\w.+-]+@[\w.-]+)',
    # Da: Nome Cognome <email@domain.com>
    r'Da:\s*([^<\n]+?)\s*<([\w.+-]+@[\w.-]+)>',
    # From: "Name" <email>
    r'From:\s*"?([^"<\n]+?)"?\s*<([\w.+-]+@[\w.-]+)>',
    # From: email@domain.com (no display name)
    r'From:\s*([\w.+-]+@[\w.-]+)',
]

_BARE_EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', re.I)


def _parse_forwarded_senders(text: str) -> list[Sender]:
    """Extract Sender objects from the body of a forwarded e-mail."""
    found: list[Sender] = []
    seen: set[str] = set()

    for pattern in _FWD_FROM_PATTERNS:
        for match in re.finditer(pattern, text, re.I | re.M):
            groups = match.groups()
            if len(groups) == 2:
                name_raw, addr = groups
                addr = addr.lower().strip()
                if addr in seen:
                    continue
                seen.add(addr)
                parts = name_raw.strip().split(None, 1)
                first = parts[0] if parts else ""
                last = parts[1] if len(parts) > 1 else ""
                found.append(Sender(email_addr=addr, first_name=first, last_name=last))
            elif len(groups) == 1:
                addr = groups[0].lower().strip()
                if addr not in seen:
                    seen.add(addr)
                    found.append(Sender(email_addr=addr))

    return found


def _split_display_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) > 1 else (parts[0], "")


# ---------------------------------------------------------------------------
# Gmail client (using google-api-python-client)
# ---------------------------------------------------------------------------

def _build_gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds_file = os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")
    token_file = os.environ.get("GOOGLE_TOKEN", "token.json")

    creds = None
    if Path(token_file).exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_file).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _get_message_body(service, msg_id: str) -> str:
    """Return decoded plain-text body (or HTML as fallback) for a message."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    def _extract(parts) -> str:
        for p in parts:
            if p.get("mimeType") == "text/plain":
                data = p.get("body", {}).get("data", "")
                if data:
                    import base64
                    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            if "parts" in p:
                result = _extract(p["parts"])
                if result:
                    return result
        return ""

    payload = msg.get("payload", {})
    parts = payload.get("parts", [payload])
    return _extract(parts)


def _header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h["name"].lower() == name_lower:
            return h["value"]
    return ""


def fetch_inbox_senders(days: int = 1, max_results: int = 100) -> list[Sender]:
    """
    Return unique Sender objects for every person who sent an email in the
    last *days* days, including real senders inside forwarded emails.
    """
    service = _build_gmail_service()

    after_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    query = f"in:inbox after:{after_ts} -from:me"

    threads = []
    page_token = None
    while len(threads) < max_results:
        kwargs = dict(userId="me", q=query, maxResults=min(50, max_results - len(threads)))
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().threads().list(**kwargs).execute()
        threads.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    seen_emails: set[str] = set()
    senders: list[Sender] = []

    # Skip our own domains / notification senders
    skip_domains = {"facebookmail.com", "googlemail.com"}

    for t in threads:
        thread_detail = service.users().threads().get(
            userId="me", id=t["id"], format="full"
        ).execute()

        for msg in thread_detail.get("messages", []):
            headers = msg.get("payload", {}).get("headers", [])
            from_raw = _header(headers, "From")
            subj = _header(headers, "Subject")

            # Parse direct From header
            realname, addr = email.utils.parseaddr(from_raw)
            addr = addr.lower().strip()

            if addr and addr not in seen_emails:
                domain = addr.split("@")[-1]
                if domain not in skip_domains:
                    seen_emails.add(addr)
                    first, last = _split_display_name(realname) if realname else ("", "")
                    senders.append(Sender(email_addr=addr, first_name=first, last_name=last, subject=subj))

            # Also scan the body for forwarded-email senders
            body = _get_message_body(service, msg["id"])
            for fwd_sender in _parse_forwarded_senders(body):
                e = fwd_sender.email_addr
                domain = e.split("@")[-1]
                if e not in seen_emails and domain not in skip_domains:
                    seen_emails.add(e)
                    fwd_sender.subject = subj
                    senders.append(fwd_sender)

    return senders


# ---------------------------------------------------------------------------
# HubSpot client (using hubspot-api-client)
# ---------------------------------------------------------------------------

def _build_hubspot_client():
    from hubspot import HubSpot

    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN env var not set")
    return HubSpot(access_token=token)


def _find_contact_by_email(client, email_addr: str) -> Optional[dict]:
    from hubspot.crm.contacts import ApiException, PublicObjectSearchRequest

    search_req = PublicObjectSearchRequest(
        filter_groups=[{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email_addr,
            }]
        }],
        properties=["email", "firstname", "lastname", "company", "lead_source",
                    "hs_lead_status"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=search_req)
        if resp.results:
            return resp.results[0]
    except ApiException as exc:
        log.warning("HubSpot search error for %s: %s", email_addr, exc)
    return None


def _upsert_contact(client, sender: Sender) -> SyncResult:
    from hubspot.crm.contacts import ApiException, SimplePublicObjectInputForCreate
    from hubspot.crm.contacts.models import SimplePublicObjectInput

    existing = _find_contact_by_email(client, sender.email_addr)

    company = sender.company_from_domain()
    props_to_set: dict[str, str] = {"lead_source": "Gmail"}
    if company:
        props_to_set["company"] = company

    if existing:
        contact_id = existing.id
        existing_props = existing.properties or {}

        updates: dict[str, str] = {}
        for key, val in props_to_set.items():
            if not existing_props.get(key):
                updates[key] = val

        if sender.first_name and not existing_props.get("firstname"):
            updates["firstname"] = sender.first_name
        if sender.last_name and not existing_props.get("lastname"):
            updates["lastname"] = sender.last_name

        if updates:
            try:
                client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input=SimplePublicObjectInput(properties=updates),
                )
                return SyncResult(sender.email_addr, "Aggiornato", contact_id)
            except ApiException as exc:
                log.error("HubSpot update error for %s: %s", sender.email_addr, exc)
                return SyncResult(sender.email_addr, "Ignorato", contact_id, str(exc))

        return SyncResult(sender.email_addr, "Ignorato", contact_id,
                          "Nessun campo mancante da aggiornare")

    # Create new contact
    all_props = {
        "email": sender.email_addr,
        "lead_source": "Gmail",
        **props_to_set,
    }
    if sender.first_name:
        all_props["firstname"] = sender.first_name
    if sender.last_name:
        all_props["lastname"] = sender.last_name

    try:
        created = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=all_props
            )
        )
        return SyncResult(sender.email_addr, "Creato", created.id)
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", sender.email_addr, exc)
        return SyncResult(sender.email_addr, "Ignorato", "", str(exc))


def _add_inbound_note(client, contact_id: str, subject: str) -> None:
    """Add a HubSpot note logging the inbound Gmail email."""
    from hubspot.crm.objects.notes import ApiException as NoteApiException
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate

    now_ms = str(int(time.time() * 1000))
    props = {
        "hs_timestamp": now_ms,
        "hs_note_body": f"[Inbound Gmail] {subject or '(nessun oggetto)'}",
    }
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props,
                associations=[{
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED",
                               "associationTypeId": 202}],
                }],
            )
        )
        log.debug("Note created: %s", note.id)
    except Exception as exc:
        log.warning("Could not create note for contact %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def run_sync(days: int = 1, add_notes: bool = True) -> list[SyncResult]:
    log.info("Fetching Gmail senders from the last %d day(s)…", days)
    senders = fetch_inbox_senders(days=days)
    log.info("Found %d unique external senders.", len(senders))

    client = _build_hubspot_client()
    results: list[SyncResult] = []

    for sender in senders:
        result = _upsert_contact(client, sender)
        log.info("[%s] %s (ID: %s)", result.status, result.email_addr, result.contact_id or "—")
        if add_notes and result.status in ("Creato", "Aggiornato") and result.contact_id:
            _add_inbound_note(client, result.contact_id, sender.subject)
        results.append(result)

    return results


def print_report(results: list[SyncResult]) -> None:
    created = [r for r in results if r.status == "Creato"]
    updated = [r for r in results if r.status == "Aggiornato"]
    ignored = [r for r in results if r.status == "Ignorato"]

    print(f"\n{'='*60}")
    print(f"  Gmail → HubSpot Sync Report  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"{'='*60}")
    print(f"  Totale processati : {len(results)}")
    print(f"  Creati            : {len(created)}")
    print(f"  Aggiornati        : {len(updated)}")
    print(f"  Ignorati          : {len(ignored)}")
    print(f"{'='*60}")

    for status_label, group in [("CREATI", created), ("AGGIORNATI", updated), ("IGNORATI", ignored)]:
        if group:
            print(f"\n  {status_label}:")
            for r in group:
                note = f"  ({r.note})" if r.note else ""
                print(f"    {r.email_addr:<45}  ID: {r.contact_id or '—'}{note}")

    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts.")
    parser.add_argument("--days", type=int, default=1,
                        help="How many days back to scan Gmail inbox (default: 1)")
    parser.add_argument("--no-notes", action="store_true",
                        help="Skip creating HubSpot notes for each email")
    args = parser.parse_args()

    results = run_sync(days=args.days, add_notes=not args.no_notes)
    print_report(results)
    sys.exit(0 if all(r.status != "Ignorato" or not r.note for r in results) else 1)
