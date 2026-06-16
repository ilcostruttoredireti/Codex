"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs senders as HubSpot contacts.
Supports both direct senders and contacts embedded in forwarded messages.

Usage:
    python gmail_hubspot_sync.py [--hours 24] [--dry-run]

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 \
                google-api-python-client hubspot-api-client python-dotenv
"""

import re
import os
import json
import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "gmail_credentials.json")

# Addresses that belong to the account owner — never imported as contacts
SELF_ADDRESSES: set[str] = {
    addr.strip().lower()
    for addr in os.getenv("SELF_ADDRESSES", "").split(",")
    if addr.strip()
}

# Patterns for addresses to skip unconditionally
SKIP_PATTERNS = [
    re.compile(r"^mailer-daemon@", re.I),
    re.compile(r"^no-?reply@", re.I),
    re.compile(r"^noreply@", re.I),
    re.compile(r"^postmaster@", re.I),
    re.compile(r"@googlemail\.com$", re.I),
    re.compile(r"@bounce\.", re.I),
]

# Matches "Da: Name <email>" or "Da Name <email>" or "Da \"Name\" email@..."
# as found in Italian-locale forwarded message headers
FWD_SENDER_RE = re.compile(
    r'Da[:\s]+"?([^<\n"]+?)"?\s+<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
    re.IGNORECASE,
)
# Bare email in "Da email@domain.com" forwarded headers
FWD_EMAIL_RE = re.compile(
    r"Da\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})",
    re.IGNORECASE,
)

# Known forwarding relay addresses — extract the real sender from the body
RELAY_ADDRESSES: set[str] = {
    addr.strip().lower()
    for addr in os.getenv(
        "RELAY_ADDRESSES",
        "redazione@latestata.it",
    ).split(",")
    if addr.strip()
} | SELF_ADDRESSES


@dataclass
class ContactInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = field(init=False)

    def __post_init__(self):
        self.email = self.email.strip().lower()
        self.domain = self.email.split("@")[-1] if "@" in self.email else ""

    @property
    def full_name(self) -> str:
        return f"{self.firstname} {self.lastname}".strip()


@dataclass
class SyncResult:
    status: str        # "created" | "updated" | "skipped"
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _body_text(msg: dict) -> str:
    """Return decoded plain-text body (best-effort)."""
    import base64

    def _extract(part):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            result = _extract(sub)
            if result:
                return result
        return ""

    return _extract(msg.get("payload", {}))


def _parse_name_email(raw: str) -> tuple[str, str]:
    """Parse 'First Last <email@domain>' or just 'email@domain'."""
    m = re.match(r'"?(.+?)"?\s*<([^>]+)>', raw)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    raw = raw.strip()
    if "@" in raw:
        return "", raw.lower()
    return "", ""


def _extract_fwd_contacts(body: str) -> list[ContactInfo]:
    """Pull sender contacts embedded in Italian-locale forwarded message headers."""
    contacts = []
    for m in FWD_SENDER_RE.finditer(body):
        name_raw, email = m.group(1).strip(), m.group(2).strip().lower()
        if _should_skip(email):
            continue
        parts = name_raw.split(None, 1)
        ci = ContactInfo(
            email=email,
            firstname=parts[0] if parts else "",
            lastname=parts[1] if len(parts) > 1 else "",
        )
        contacts.append(ci)
    # Bare "Da email@..." without a display name
    for m in FWD_EMAIL_RE.finditer(body):
        email = m.group(1).strip().lower()
        if _should_skip(email) or any(c.email == email for c in contacts):
            continue
        contacts.append(ContactInfo(email=email))
    return contacts


def _should_skip(email: str) -> bool:
    email = email.lower()
    if email in SELF_ADDRESSES:
        return True
    return any(p.search(email) for p in SKIP_PATTERNS)


def fetch_new_senders(service, hours: int = 24) -> list[ContactInfo]:
    """Return unique ContactInfo objects for all external senders in the given window."""
    after = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
    query = f"in:inbox after:{after} -from:me"
    results = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=100)
        .execute()
    )
    messages = results.get("messages", [])
    seen: dict[str, ContactInfo] = {}

    for item in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=item["id"], format="full")
            .execute()
        )
        from_raw = _header(msg, "From")
        name_raw, email = _parse_name_email(from_raw)

        if not email or _should_skip(email):
            continue

        if email.lower() in RELAY_ADDRESSES:
            # Extract real senders from forwarded body
            body = _body_text(msg)
            for ci in _extract_fwd_contacts(body):
                if ci.email not in seen:
                    seen[ci.email] = ci
        else:
            if email not in seen:
                parts = name_raw.split(None, 1)
                seen[email] = ContactInfo(
                    email=email,
                    firstname=parts[0] if parts else "",
                    lastname=parts[1] if len(parts) > 1 else "",
                )

    return list(seen.values())


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_client():
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def _find_contact(client, email: str) -> Optional[dict]:
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    res = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    return res.results[0].to_dict() if res.results else None


def _domain_to_company(domain: str) -> str:
    """Derive a readable company name from an email domain."""
    # Strip common suffixes like .it, .com, .org; capitalise
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def sync_contact(client, ci: ContactInfo, dry_run: bool = False) -> SyncResult:
    existing = _find_contact(client, ci.email)

    props_to_set: dict[str, str] = {"leadsource": "Gmail"}

    if existing:
        hs_id = existing["id"]
        existing_props = existing.get("properties", {})
        updates: dict[str, str] = {}

        if ci.firstname and not existing_props.get("firstname"):
            updates["firstname"] = ci.firstname
        if ci.lastname and not existing_props.get("lastname"):
            updates["lastname"] = ci.lastname
        if ci.company and not existing_props.get("company"):
            updates["company"] = ci.company
        if not existing_props.get("leadsource"):
            updates["leadsource"] = "Gmail"

        if not updates:
            return SyncResult("skipped", ci.email, hs_id, "no missing fields")

        if not dry_run:
            client.crm.contacts.basic_api.update(
                contact_id=hs_id,
                simple_public_object_input=hubspot.crm.contacts.models.SimplePublicObjectInput(
                    properties=updates
                ),
            )
        return SyncResult("updated", ci.email, hs_id, f"fields: {list(updates)}")

    # Create new contact
    company = ci.company or _domain_to_company(ci.domain) if "@" not in ci.domain else ""
    props_to_set.update(
        {
            "email": ci.email,
            "firstname": ci.firstname,
            "lastname": ci.lastname,
            "company": company,
            "leadsource": "Gmail",
        }
    )
    props_to_set = {k: v for k, v in props_to_set.items() if v}

    if not dry_run:
        resp = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props_to_set
            )
        )
        hs_id = resp.id
    else:
        hs_id = "dry-run"

    return SyncResult("created", ci.email, hs_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument("--hours", type=int, default=24, help="Look-back window in hours")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing")
    args = parser.parse_args()

    gmail = _get_gmail_service()
    contacts = fetch_new_senders(gmail, hours=args.hours)
    log.info("Found %d unique external senders in the last %dh", len(contacts), args.hours)

    hs = _hs_client()
    results: list[SyncResult] = []
    for ci in contacts:
        try:
            result = sync_contact(hs, ci, dry_run=args.dry_run)
            results.append(result)
            log.info("[%s] %s (HS ID: %s) — %s", result.status.upper(), ci.email, result.hubspot_id, result.reason)
        except ApiException as exc:
            log.error("HubSpot error for %s: %s", ci.email, exc)
            results.append(SyncResult("error", ci.email, reason=str(exc)))

    print("\n--- Sync Report ---")
    print(f"{'Status':<10} {'Email':<55} {'HubSpot ID'}")
    print("-" * 90)
    for r in results:
        print(f"{r.status:<10} {r.email:<55} {r.hubspot_id or ''}")

    totals = {s: sum(1 for r in results if r.status == s) for s in ("created", "updated", "skipped", "error")}
    print(f"\nCreated: {totals['created']}  Updated: {totals['updated']}  "
          f"Skipped: {totals['skipped']}  Errors: {totals['error']}")

    return json.dumps([r.__dict__ for r in results], indent=2)


if __name__ == "__main__":
    main()
