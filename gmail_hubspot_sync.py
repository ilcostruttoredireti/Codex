#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Fetches inbox emails, extracts real sender contacts (skipping no-reply/automated
addresses), and upserts them into HubSpot using email as the dedup key.

Usage:
    python gmail_hubspot_sync.py

Required env vars:
    HUBSPOT_ACCESS_TOKEN   HubSpot private app token with crm.objects.contacts.write scope
    GMAIL_TOKEN_PATH       Path to OAuth token file (default: token.json)
    GMAIL_CREDENTIALS_PATH Path to OAuth credentials JSON (default: credentials.json)

Output (JSON lines to stdout):
    {"status": "Creato"|"Aggiornato"|"Ignorato", "email": "...", "contact_id": "..."}
"""

import json
import logging
import os
import pickle
import sys
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
)

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox -from:me newer_than:1d")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "100"))

HUBSPOT_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
GMAIL_TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "token.json")
GMAIL_CREDS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")

# Local prefixes that indicate automated/no-reply senders
_SKIP_LOCAL = {
    "no-reply", "noreply", "do-not-reply", "donotreply",
    "nobody", "mailer-daemon", "postmaster", "bounce",
    "notifications", "alerts", "updates", "info-noreply",
    "system", "automated", "auto",
}

log = logging.getLogger(__name__)


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_PATH):
        with open(GMAIL_TOKEN_PATH, "rb") as fh:
            creds = pickle.load(fh)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_PATH, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_PATH, "wb") as fh:
            pickle.dump(creds, fh)

    return build("gmail", "v1", credentials=creds)


def _is_automated(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(local.startswith(p) or local == p for p in _SKIP_LOCAL)


def fetch_senders(service) -> list[dict]:
    """Return unique non-automated {email, raw_sender} dicts from inbox threads."""
    resp = service.users().threads().list(
        userId="me", q=GMAIL_QUERY, maxResults=MAX_RESULTS
    ).execute()
    threads = resp.get("threads", [])

    seen: dict[str, str] = {}
    for t in threads:
        thread = service.users().threads().get(
            userId="me", id=t["id"],
            format="metadata",
            metadataHeaders=["From"],
        ).execute()
        for msg in thread.get("messages", []):
            for hdr in msg.get("payload", {}).get("headers", []):
                if hdr["name"] == "From":
                    _, addr = parseaddr(hdr["value"])
                    addr = addr.lower().strip()
                    if addr and "@" in addr and not _is_automated(addr) and addr not in seen:
                        seen[addr] = hdr["value"]

    return [{"email": e, "raw_sender": raw} for e, raw in seen.items()]


# ── Contact parsing ───────────────────────────────────────────────────────────

def _parse_name(raw_sender: str, email: str) -> tuple[str, str]:
    """Return (firstname, lastname) from a raw From header."""
    display, _ = parseaddr(raw_sender)
    if not display:
        display = email.split("@")[0].replace(".", " ").replace("_", " ").title()
    parts = display.strip().split(maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _company_from_domain(email: str) -> str:
    domain = email.split("@")[-1]
    apex = domain.split(".")[-2] if domain.count(".") >= 1 else domain
    return apex.replace("-", " ").replace("_", " ").title()


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def _find_by_email(client, email: str):
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[Filter(
            property_name="email", operator="EQ", value=email
        )])],
        properties=["email", "firstname", "lastname", "company",
                    "hs_analytics_source_data_1"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(
        public_object_search_request=req
    )
    return resp.results[0] if resp.total > 0 else None


def _create(client, email: str, first: str, last: str, company: str):
    props = {
        "email": email,
        "firstname": first,
        "company": company,
        # hs_analytics_source is writable at creation; drilldown fields are read-only post-create
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if last:
        props["lastname"] = last
    body = SimplePublicObjectInputForCreate(properties=props, associations=[])
    return client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=body
    )


def _update(client, contact_id: str, updates: dict):
    body = SimplePublicObjectInput(properties=updates)
    return client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=body,
    )


# ── Core sync ────────────────────────────────────────────────────────────────

def sync_one(client, raw_sender: str, email: str) -> dict:
    first, last = _parse_name(raw_sender, email)
    company = _company_from_domain(email)

    existing = _find_by_email(client, email)

    if existing:
        cid = existing.id
        props = existing.properties
        updates = {}
        if not props.get("firstname") and first:
            updates["firstname"] = first
        if not props.get("lastname") and last:
            updates["lastname"] = last
        if not props.get("company"):
            updates["company"] = company
        # hs_analytics_source_data_1 is read-only on existing records; skip update

        if updates:
            _update(client, cid, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        result = _create(client, email, first, last, company)
        cid = result.id
        status = "Creato"

    return {"status": status, "email": email, "contact_id": cid}


def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    log.info("Starting Gmail → HubSpot sync  query=%s", GMAIL_QUERY)

    service = _gmail_service()
    client = _hs_client()

    senders = fetch_senders(service)
    log.info("Found %d unique non-automated senders", len(senders))

    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for s in senders:
        try:
            r = sync_one(client, s["raw_sender"], s["email"])
            counts[r["status"]] += 1
            log.info("[%s] %s  id=%s", r["status"], r["email"], r["contact_id"])
            print(json.dumps(r))
        except ApiException as exc:
            log.error("HubSpot error for %s: %s", s["email"], exc)
            counts["Errore"] += 1
            print(json.dumps({"status": "Errore", "email": s["email"], "contact_id": None}))

    log.info("Done: %s", counts)


if __name__ == "__main__":
    run()
