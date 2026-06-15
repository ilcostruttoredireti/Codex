"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and upserts sender contacts in HubSpot.

Required env vars:
  HUBSPOT_ACCESS_TOKEN  — HubSpot private app token
  GOOGLE_CREDENTIALS    — path to OAuth2 credentials JSON (or use Application Default Credentials)

Usage:
  python gmail_hubspot_sync.py               # process last 24h
  python gmail_hubspot_sync.py --hours 48    # lookback window in hours
  python gmail_hubspot_sync.py --dry-run     # print actions without writing to HubSpot
"""

import argparse
import base64
import email as email_lib
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# --- optional deps; install with: pip install google-auth-httplib2 google-api-python-client hubspot-api-client ---
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

try:
    from hubspot import HubSpot
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate, PublicObjectSearchRequest
    from hubspot.crm.contacts.models import Filter, FilterGroup
    HUBSPOT_AVAILABLE = True
except ImportError:
    HUBSPOT_AVAILABLE = False

# Gmail OAuth scopes (read-only inbox)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Addresses owned by this account — never synced as external contacts
SELF_ADDRESSES: set[str] = {
    "cristian.mameli.editore@gmail.com",
    "redazione@latestata.it",
    "pubblica.latestata@gmail.com",
}

CONTACT_SOURCE_LABEL = "Gmail"
INBOUND_TAG = "Inbound Gmail"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ContactInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    thread_subject: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""

    def company_from_domain(self) -> str:
        """Derive a best-guess company name from the email domain."""
        domain = self.domain
        # strip common public providers
        public = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                  "libero.it", "virgilio.it", "alice.it", "tiscali.it"}
        if domain in public:
            return ""
        # strip TLD and country code, capitalise
        parts = domain.split(".")
        # remove last 1–2 segments (TLD / country code)
        core = parts[:-2] if len(parts) > 2 else parts[:1]
        return " ".join(p.capitalize() for p in core)


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato" | "Errore"
    email: str
    hubspot_id: Optional[str] = None
    note: str = ""


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _gmail_service():
    """Build an authenticated Gmail API service."""
    creds = None
    token_path = "token.json"
    creds_path = os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _parse_sender(from_header: str) -> tuple[str, str, str]:
    """Return (email, first_name, last_name) parsed from a From header."""
    # e.g. "Mario Rossi <mario@example.com>" or "mario@example.com"
    match = re.match(r"^(.+?)\s*<([^>]+)>$", from_header.strip())
    if match:
        display, addr = match.group(1).strip().strip('"'), match.group(2).strip()
        parts = display.split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        return addr.lower(), first, last
    addr = from_header.strip().lower()
    return addr, "", ""


def _extract_embedded_sender(snippet: str) -> Optional[tuple[str, str, str]]:
    """
    Many messages are forwarded wrappers where the real sender appears in the
    body as 'Da: Name <email>' or 'From: Name <email>'.
    Returns (email, first_name, last_name) or None.
    """
    # Italian "Da:" or English "From:" prefix in the plaintext snippet
    pattern = re.compile(
        r"(?:Da|From):\s*[\"']?(.+?)[\"']?\s*[<(]([^>)]+@[^>)]+)[>)]",
        re.IGNORECASE,
    )
    m = pattern.search(snippet)
    if not m:
        # try bare email after Da:
        pattern2 = re.compile(r"(?:Da|From):\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")
        m2 = pattern2.search(snippet)
        if m2:
            addr = m2.group(1).lower()
            return addr, "", ""
        return None
    display, addr = m.group(1).strip(), m.group(2).strip().lower()
    parts = display.split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return addr, first, last


def fetch_inbox_senders(hours: int = 24) -> list[ContactInfo]:
    """Return deduplicated ContactInfo list from inbox messages in the last `hours`."""
    if not GMAIL_AVAILABLE:
        raise RuntimeError("google-api-python-client not installed. Run: pip install google-auth-httplib2 google-api-python-client google-auth-oauthlib")

    svc = _gmail_service()
    query = f"in:inbox newer_than:{hours}h -from:me -in:draft"
    response = svc.users().threads().list(userId="me", q=query, maxResults=50).execute()
    threads = response.get("threads", [])

    seen: dict[str, ContactInfo] = {}

    for t in threads:
        thread_data = svc.users().threads().get(userId="me", threadId=t["id"],
                                                format="metadata",
                                                metadataHeaders=["From", "Subject"]).execute()
        for msg in thread_data.get("messages", []):
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from_hdr = headers.get("From", "")
            subject = headers.get("Subject", "")
            snippet = msg.get("snippet", "")

            addr, first, last = _parse_sender(from_hdr)

            # skip self-addresses
            if addr in SELF_ADDRESSES:
                # try to extract embedded real sender from forwarded body
                embedded = _extract_embedded_sender(snippet)
                if embedded:
                    addr, first, last = embedded
                else:
                    continue

            if not addr or "@" not in addr:
                continue
            if addr in SELF_ADDRESSES:
                continue

            if addr not in seen:
                c = ContactInfo(email=addr, first_name=first, last_name=last,
                                thread_subject=subject)
                c.company = c.company_from_domain()
                seen[addr] = c
            else:
                # enrich existing record if we now have a name
                c = seen[addr]
                if not c.first_name and first:
                    c.first_name = first
                if not c.last_name and last:
                    c.last_name = last

    return list(seen.values())


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_client() -> "HubSpot":
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("Set HUBSPOT_ACCESS_TOKEN environment variable.")
    return HubSpot(access_token=token)


def find_contact_by_email(client: "HubSpot", email: str) -> Optional[dict]:
    """Return the existing HubSpot contact dict or None."""
    filt = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[filt])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company",
                    "hs_analytics_source_data_1"],
        limit=1,
    )
    result = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    if result.total > 0:
        return result.results[0]
    return None


def upsert_contact(client: "HubSpot", info: ContactInfo,
                   dry_run: bool = False) -> SyncResult:
    """Create or update a HubSpot contact. Returns a SyncResult."""
    existing = find_contact_by_email(client, info.email)

    props: dict[str, str] = {"email": info.email}
    if info.first_name:
        props["firstname"] = info.first_name
    if info.last_name:
        props["lastname"] = info.last_name
    if info.company:
        props["company"] = info.company
    # source tracking
    props["hs_analytics_source_data_1"] = CONTACT_SOURCE_LABEL

    if existing:
        contact_id = existing.id
        existing_props = existing.properties or {}

        # only patch fields that are currently empty
        update_props: dict[str, str] = {}
        for k, v in props.items():
            if k == "email":
                continue
            if not existing_props.get(k):
                update_props[k] = v

        if not update_props:
            return SyncResult(status="Ignorato", email=info.email, hubspot_id=contact_id)

        if not dry_run:
            from hubspot.crm.contacts import SimplePublicObjectInput
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=update_props),
            )
        return SyncResult(status="Aggiornato", email=info.email, hubspot_id=contact_id,
                          note=f"Updated: {list(update_props.keys())}")
    else:
        if not dry_run:
            obj = SimplePublicObjectInputForCreate(properties=props)
            created = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=obj)
            contact_id = created.id
        else:
            contact_id = "DRY_RUN"
        return SyncResult(status="Creato", email=info.email, hubspot_id=contact_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_sync(hours: int = 24, dry_run: bool = False) -> list[SyncResult]:
    if not GMAIL_AVAILABLE:
        print("[ERROR] Gmail library not available. Install: pip install google-auth-httplib2 google-api-python-client google-auth-oauthlib", file=sys.stderr)
        return []
    if not HUBSPOT_AVAILABLE:
        print("[ERROR] HubSpot library not available. Install: pip install hubspot-api-client", file=sys.stderr)
        return []

    print(f"[{datetime.now(timezone.utc).isoformat()}] Fetching Gmail inbox (last {hours}h)…")
    contacts = fetch_inbox_senders(hours=hours)
    print(f"  Found {len(contacts)} unique external sender(s).")

    if not contacts:
        print("  Nothing to sync.")
        return []

    hs = _hs_client()
    results: list[SyncResult] = []

    for c in contacts:
        try:
            r = upsert_contact(hs, c, dry_run=dry_run)
        except Exception as exc:
            r = SyncResult(status="Errore", email=c.email, note=str(exc))
        results.append(r)

    return results


def print_report(results: list[SyncResult]) -> None:
    print("\n── Sync Report ─────────────────────────────────────────────────")
    col = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        col[r.status] = col.get(r.status, 0) + 1
        note = f"  ({r.note})" if r.note else ""
        hs_id = r.hubspot_id or "—"
        print(f"  {r.status:<12} {r.email:<45} HS:{hs_id}{note}")
    print("────────────────────────────────────────────────────────────────")
    print(f"  Creati: {col['Creato']}  Aggiornati: {col['Aggiornato']}  "
          f"Ignorati: {col['Ignorato']}  Errori: {col['Errore']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Gmail inbound senders to HubSpot contacts.")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window in hours (default: 24)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing to HubSpot")
    args = parser.parse_args()

    results = run_sync(hours=args.hours, dry_run=args.dry_run)
    print_report(results)
    has_errors = any(r.status == "Errore" for r in results)
    sys.exit(1 if has_errors else 0)
