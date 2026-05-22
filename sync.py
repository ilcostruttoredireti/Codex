#!/usr/bin/env python3
"""Gmail → HubSpot contact sync daemon. Polls inbox and upserts contacts."""

import json
import logging
import os
import re
import time
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "processed_ids.json"))
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox -from:me")
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "50"))
SKIP_EMAILS = frozenset(e.strip() for e in os.getenv("SKIP_EMAILS", "").split(",") if e.strip())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── State ─────────────────────────────────────────────────────────────────────

def load_processed_ids() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_processed_ids(ids: set) -> None:
    # Keep only the most recent 20 000 to prevent unbounded growth
    recent = sorted(ids)[-20_000:]
    STATE_FILE.write_text(json.dumps(recent))


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    token_path = Path("token.json")
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# Italian / English forwarded-header patterns (Da "Name" <email> or From Name email)
_FWD_RE = [
    re.compile(r'(?:Da|From)\s+"([^"]+)"\s+<?([\w.%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>?', re.I),
    re.compile(r'(?:Da|From)\s+([A-Za-z][^<\n]{1,60}?)\s+<?([\w.%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>?', re.I),
]

# Personal free-email domains — no company can be inferred from them
_FREE_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "libero.it", "virgilio.it", "icloud.com", "outlook.com", "live.com",
    "tiscali.it", "alice.it", "tin.it",
})


def extract_contacts(headers: dict, snippet: str) -> list[dict]:
    """Returns [{email, firstname, lastname, company}] from a Gmail message."""
    contacts = []

    sender_raw = headers.get("From", "")
    name, email = parseaddr(sender_raw)
    if email and "@" in email:
        contacts.append(_build(email, name))

    # Try to recover original sender from forwarded snippet
    for pattern in _FWD_RE:
        m = pattern.search(snippet)
        if m:
            fwd_name, fwd_email = m.group(1).strip(), m.group(2).strip()
            if fwd_email and "@" in fwd_email and fwd_email.lower() != email.lower():
                contacts.append(_build(fwd_email, fwd_name))
            break

    return contacts


def _build(email: str, display_name: str) -> dict:
    email = email.strip().lower()
    domain = email.split("@")[1] if "@" in email else ""
    company = "" if domain in _FREE_DOMAINS else _domain_to_company(domain)
    first, last = _split_name(display_name, domain)
    return {"email": email, "firstname": first, "lastname": last, "company": company}


def _split_name(name: str, domain: str = "") -> tuple[str, str]:
    name = name.strip()
    # Looks like an email address or empty — skip
    if not name or "@" in name:
        return "", ""
    # Role-style names (Ufficio Stampa, Press Office…) → put whole string in firstname
    role_words = {"ufficio", "office", "press", "stampa", "media", "info", "news", "redazione"}
    first_word = name.split()[0].lower()
    if first_word in role_words:
        return name, ""
    parts = name.split()
    return parts[0], " ".join(parts[1:]) if len(parts) > 1 else ""


def _domain_to_company(domain: str) -> str:
    """Turns a domain like wemakefuture.it into WeMakeFuture."""
    if not domain:
        return ""
    # Strip TLD (last segment) and common subdomain prefixes
    parts = domain.split(".")
    # Drop known TLD suffixes
    while parts and parts[-1] in ("it", "com", "org", "net", "eu", "ch", "mt"):
        parts.pop()
    if not parts:
        return domain
    name = parts[-1]  # use the second-level domain
    return name.replace("-", " ").title()


def fetch_new_messages(service, processed_ids: set) -> list[dict]:
    result = service.users().messages().list(
        userId="me", q=GMAIL_QUERY, maxResults=GMAIL_MAX_RESULTS
    ).execute()
    new = [m for m in result.get("messages", []) if m["id"] not in processed_ids]

    full = []
    for m in new:
        try:
            full.append(
                service.users().messages().get(
                    userId="me",
                    id=m["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
            )
        except Exception as e:
            log.warning("Could not fetch %s: %s", m["id"], e)
    return full


# ── HubSpot ───────────────────────────────────────────────────────────────────

def get_hubspot_client():
    return hubspot.Client.create(access_token=os.environ["HUBSPOT_ACCESS_TOKEN"])


def upsert_contact(hs, contact: dict) -> tuple[str, str]:
    """Returns ('created'|'updated'|'skipped', contact_id)."""
    email = contact["email"]
    if not email or email in SKIP_EMAILS:
        return "skipped", ""

    base_props = {
        "email": email,
        "leadsource": "Gmail",
        "hs_lead_status": "NEW",
    }
    if contact["firstname"]:
        base_props["firstname"] = contact["firstname"]
    if contact["lastname"]:
        base_props["lastname"] = contact["lastname"]
    if contact["company"]:
        base_props["company"] = contact["company"]

    try:
        existing = hs.crm.contacts.basic_api.get_by_id(
            contact_id=email,
            id_property="email",
            properties=["firstname", "lastname", "company"],
        )
        cid = existing.id
        patch = {
            k: v
            for k, v in base_props.items()
            if k not in ("email",) and not (existing.properties.get(k))
        }
        patch["leadsource"] = "Gmail"
        hs.crm.contacts.basic_api.update(
            contact_id=cid,
            simple_public_object_input=SimplePublicObjectInput(properties=patch),
        )
        return "updated", cid

    except ApiException as exc:
        if exc.status != 404:
            raise
        created = hs.crm.contacts.basic_api.create(
            simple_public_object_input=SimplePublicObjectInput(properties=base_props)
        )
        return "created", created.id


# ── Main loop ─────────────────────────────────────────────────────────────────

def sync_once(gmail_svc, hs_client, processed_ids: set) -> set:
    messages = fetch_new_messages(gmail_svc, processed_ids)
    if not messages:
        log.info("No new messages.")
        return processed_ids

    seen: set[str] = set()
    for msg in messages:
        processed_ids.add(msg["id"])
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        snippet = msg.get("snippet", "")
        for contact in extract_contacts(headers, snippet):
            email = contact["email"]
            if email in seen:
                continue
            seen.add(email)
            try:
                status, cid = upsert_contact(hs_client, contact)
                log.info("[%-8s] %-45s  hubspot_id=%s", status.upper(), email, cid)
            except Exception as exc:
                log.error("[ERROR   ] %s: %s", email, exc)

    save_processed_ids(processed_ids)
    return processed_ids


def main():
    gmail = get_gmail_service()
    hs = get_hubspot_client()
    processed_ids = load_processed_ids()

    log.info("Sync daemon started — polling every %ss", POLL_INTERVAL)
    while True:
        try:
            processed_ids = sync_once(gmail, hs, processed_ids)
        except Exception as exc:
            log.error("Cycle failed: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
