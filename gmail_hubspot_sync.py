"""
Gmail → HubSpot Contact Sync
Scheduled routine: monitors inbox, extracts senders, syncs contacts to HubSpot.

Requires:
  pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client

Env vars:
  GOOGLE_CREDENTIALS_JSON  – path to OAuth2 credentials file
  GOOGLE_TOKEN_JSON        – path to token file (auto-created on first run)
  HUBSPOT_ACCESS_TOKEN     – HubSpot private app token
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
# Maps to HubSpot's standard "Original Traffic Source" (hs_analytics_source).
# Valid values: DIRECT_TRAFFIC | ORGANIC_SEARCH | PAID_SEARCH | EMAIL_MARKETING
#               SOCIAL_MEDIA | REFERRALS | OTHER_CAMPAIGNS | OFFLINE | ORGANIC_SOCIAL
# Note: hs_analytics_source_data_1/2 are read-only in HubSpot — set only the parent.
HUBSPOT_LEAD_SOURCE_PROP = "hs_analytics_source"
HUBSPOT_LEAD_SOURCE_VAL = "EMAIL_MARKETING"
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "1"))

GENERIC_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "libero.it",
                   "outlook.com", "live.com", "icloud.com", "tiscali.it"}


# ── Gmail ────────────────────────────────────────────────────────────────────

def gmail_service():
    creds = None
    token_path = os.environ.get("GOOGLE_TOKEN_JSON", "token.json")
    creds_path = os.environ.get("GOOGLE_CREDENTIALS_JSON", "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_senders(service, lookback_days: int) -> list[dict]:
    """Return list of {email, name, domain} dicts for each unique inbox sender."""
    after = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y/%m/%d")
    query = f"in:inbox after:{after} -from:me"
    senders: dict[str, dict] = {}

    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])

        for msg in messages:
            meta = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From"]
            ).execute()

            headers = {h["name"]: h["value"]
                       for h in meta.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            name, addr = parseaddr(raw_from)
            addr = addr.lower().strip()

            if not addr or "@" not in addr or addr in senders:
                continue

            domain = addr.split("@")[1]
            company = None if domain in GENERIC_DOMAINS else _domain_to_company(domain)
            senders[addr] = {"email": addr, "name": name.strip(), "domain": domain,
                             "company": company}

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return list(senders.values())


def _domain_to_company(domain: str) -> str:
    """Best-effort: strip TLD suffixes to get a human-readable company name."""
    parts = domain.split(".")
    # Drop last 1-2 TLD parts and join the rest
    meaningful = parts[:-2] if len(parts) > 2 else parts[:1]
    return " ".join(p.replace("-", " ").title() for p in meaningful)


# ── HubSpot ──────────────────────────────────────────────────────────────────

def hubspot_client():
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return hubspot.Client.create(access_token=token)


def find_contact(client, email: str) -> dict | None:
    """Return existing HubSpot contact dict or None."""
    filt = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[filt])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", HUBSPOT_LEAD_SOURCE_PROP],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.total and resp.results:
        c = resp.results[0]
        return {"id": c.id, "properties": c.properties}
    return None


def _parse_name(raw_name: str) -> tuple[str, str]:
    """Split 'Firstname Lastname' → (firstname, lastname)."""
    parts = raw_name.strip().split(None, 1) if raw_name else []
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def sync_contact(client, sender: dict) -> tuple[str, str, str]:
    """
    Sync a single sender to HubSpot.
    Returns (status, email, hubspot_id) where status ∈ {Creato, Aggiornato, Ignorato}.
    """
    email = sender["email"]
    existing = find_contact(client, email)

    first, last = _parse_name(sender.get("name", ""))

    props: dict[str, str] = {HUBSPOT_LEAD_SOURCE_PROP: HUBSPOT_LEAD_SOURCE_VAL}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if sender.get("company"):
        props["company"] = sender["company"]

    if existing:
        contact_id = existing["id"]
        ep = existing.get("properties", {})
        # Always refresh source field; only patch missing name/company
        patch = {k: v for k, v in props.items()
                 if k == HUBSPOT_LEAD_SOURCE_PROP or not ep.get(k) or ep[k] in ("", None)}
        if patch:
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
                    properties=patch
                ),
            )
        return ("Aggiornato", email, contact_id)

    # Create new contact
    new_props = {**props, "email": email}
    result = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=new_props,
            associations=[],
        )
    )
    return ("Creato", email, result.id)


# ── Entry point ───────────────────────────────────────────────────────────────

def run():
    log.info("Starting Gmail → HubSpot sync (lookback=%d days)", LOOKBACK_DAYS)
    gmail = gmail_service()
    client = hubspot_client()

    senders = fetch_inbox_senders(gmail, LOOKBACK_DAYS)
    log.info("Found %d unique senders", len(senders))

    results = []
    for sender in senders:
        try:
            status, email, contact_id = sync_contact(client, sender)
            results.append({"stato": status, "email": email, "id_hubspot": contact_id})
            log.info("[%s] %s → %s", status, email, contact_id)
        except ApiException as exc:
            log.error("HubSpot error for %s: %s", sender["email"], exc)
            results.append({"stato": "Errore", "email": sender["email"], "id_hubspot": None})
        except Exception as exc:
            log.error("Unexpected error for %s: %s", sender["email"], exc)
            results.append({"stato": "Errore", "email": sender["email"], "id_hubspot": None})

    # Summary
    created  = sum(1 for r in results if r["stato"] == "Creato")
    updated  = sum(1 for r in results if r["stato"] == "Aggiornato")
    errors   = sum(1 for r in results if r["stato"] == "Errore")
    log.info("Done — Creati: %d | Aggiornati: %d | Errori: %d", created, updated, errors)

    print("\n=== REPORT ===")
    print(f"{'STATO':<12} {'EMAIL':<45} {'ID HUBSPOT'}")
    print("-" * 75)
    for r in results:
        print(f"{r['stato']:<12} {r['email']:<45} {r['id_hubspot'] or '—'}")
    print(f"\nTotale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Errori: {errors}")

    return results


if __name__ == "__main__":
    run()
