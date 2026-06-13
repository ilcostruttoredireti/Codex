#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts sender contacts, syncs to HubSpot.
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from email.utils import parseaddr

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "in:inbox -from:me -from:mailer-daemon -from:noreply -from:no-reply -from:notification")
GMAIL_LOOKBACK_DAYS = int(os.getenv("GMAIL_LOOKBACK_DAYS", "7"))

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

STATE_FILE = os.getenv("STATE_FILE", ".gmail_sync_state.json")

# Domains to skip (automated senders)
SKIP_DOMAINS = {
    "facebookmail.com", "google.com", "googlemail.com",
    "gmail.com.s10.bounces.google.com", "accounts.google.com",
    "notifications.google.com", "mailer-daemon", "bounce",
}
SKIP_PATTERNS = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|mailer-daemon|"
    r"notification|bounce|alert|auto-reply|autoresponder|unsubscribe)@",
    re.IGNORECASE,
)

# Pattern to extract forwarded-from email addresses from snippet/body
FORWARDED_FROM_RE = re.compile(
    r"Da[:\s]+\"?([^\"<\n]+?)\"?\s*[<\[]([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})[>\]]",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("gmail-hubspot-sync")


# ──────────────────────────────────────────────────────────────────────────────
# Gmail helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = Path(GMAIL_TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_threads(service, lookback_days: int) -> list[dict]:
    """Return threads from the inbox newer than lookback_days."""
    query = f"{GMAIL_QUERY} newer_than:{lookback_days}d"
    threads, page_token = [], None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().threads().list(**kwargs).execute()
        threads.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return threads


def get_thread_messages(service, thread_id: str) -> list[dict]:
    data = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"]
    ).execute()
    return data.get("messages", [])


def extract_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Return (display_name, email, domain)."""
    display_name, email = parseaddr(from_header)
    email = email.strip().lower()
    domain = email.split("@")[-1] if "@" in email else ""
    return display_name.strip(), email, domain


def extract_forwarded_contacts(snippet: str) -> list[tuple[str, str]]:
    """Extract (name, email) pairs embedded in Italian-forwarded snippets."""
    results = []
    for m in FORWARDED_FROM_RE.finditer(snippet):
        name = m.group(1).strip().strip('"')
        email = m.group(2).strip().lower()
        if email and "@" in email:
            results.append((name, email))
    return results


def should_skip(email: str, domain: str) -> bool:
    if not email or "@" not in email:
        return True
    if any(skip in domain for skip in SKIP_DOMAINS):
        return True
    if SKIP_PATTERNS.search(email):
        return True
    return False


def company_from_domain(domain: str) -> str:
    """Derive a rough company name from an email domain."""
    if not domain or domain.endswith(".gmail.com") or domain == "gmail.com":
        return ""
    if domain.endswith(".libero.it") or domain == "libero.it":
        return ""
    # Strip TLD(s) and capitalise
    parts = domain.split(".")
    # drop last 1-2 segments (tld / sld like .gov.it)
    meaningful = parts[:-2] if len(parts) > 2 and parts[-2] in ("gov", "com", "org", "net", "edu") else parts[:-1]
    return " ".join(p.capitalize() for p in meaningful if p)


# ──────────────────────────────────────────────────────────────────────────────
# HubSpot helpers
# ──────────────────────────────────────────────────────────────────────────────

HS_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_API_KEY}",
    "Content-Type": "application/json",
}


def hs_get(path: str, params: dict = None):
    resp = requests.get(f"{HUBSPOT_BASE_URL}{path}", headers=HS_HEADERS, params=params)
    resp.raise_for_status()
    return resp.json()


def hs_post(path: str, body: dict):
    resp = requests.post(f"{HUBSPOT_BASE_URL}{path}", headers=HS_HEADERS, json=body)
    return resp


def hs_patch(path: str, body: dict):
    resp = requests.patch(f"{HUBSPOT_BASE_URL}{path}", headers=HS_HEADERS, json=body)
    return resp


def find_contact_by_email(email: str) -> dict | None:
    body = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "leadsource"],
        "limit": 1,
    }
    resp = hs_post("/crm/v3/objects/contacts/search", body)
    if resp.status_code == 200:
        results = resp.json().get("results", [])
        return results[0] if results else None
    return None


def build_contact_properties(
    name: str, email: str, domain: str, *, existing: dict | None = None
) -> dict:
    """Build the HubSpot properties dict, only filling blank fields."""
    props = {}
    existing_props = (existing or {}).get("properties", {})

    # Parse first/last name
    parts = name.split(maxsplit=1) if name else []
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""

    if firstname and not existing_props.get("firstname"):
        props["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        props["lastname"] = lastname
    if not existing_props.get("company"):
        company = company_from_domain(domain)
        if company:
            props["company"] = company
    if not existing_props.get("leadsource"):
        props["leadsource"] = "Gmail"

    return props


def create_contact(name: str, email: str, domain: str) -> dict:
    parts = name.split(maxsplit=1) if name else []
    props = {
        "email": email,
        "leadsource": "Gmail",
    }
    if parts:
        props["firstname"] = parts[0]
    if len(parts) > 1:
        props["lastname"] = parts[1]
    company = company_from_domain(domain)
    if company:
        props["company"] = company

    resp = hs_post("/crm/v3/objects/contacts", {"properties": props})
    if resp.status_code == 201:
        return resp.json()
    raise RuntimeError(f"Create failed ({resp.status_code}): {resp.text}")


def update_contact(contact_id: str, props: dict) -> dict:
    resp = hs_patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})
    if resp.status_code == 200:
        return resp.json()
    raise RuntimeError(f"Update failed ({resp.status_code}): {resp.text}")


# ──────────────────────────────────────────────────────────────────────────────
# State tracking (avoid reprocessing)
# ──────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    path = Path(STATE_FILE)
    if path.exists():
        return json.loads(path.read_text())
    return {"processed_thread_ids": [], "last_run": None}


def save_state(state: dict):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# Main sync loop
# ──────────────────────────────────────────────────────────────────────────────

def sync_once(dry_run: bool = False) -> list[dict]:
    state = load_state()
    processed_ids = set(state.get("processed_thread_ids", []))

    service = get_gmail_service()
    threads = fetch_inbox_threads(service, GMAIL_LOOKBACK_DAYS)
    log.info("Fetched %d threads from Gmail", len(threads))

    # Collect all (name, email, domain) to process — deduplicated
    candidates: dict[str, tuple[str, str]] = {}  # email → (name, domain)

    for thread in threads:
        thread_id = thread["id"]
        if thread_id in processed_ids:
            continue

        messages = get_thread_messages(service, thread_id)
        for msg in messages:
            headers = msg.get("payload", {}).get("headers", [])
            snippet = msg.get("snippet", "")
            from_header = extract_header(headers, "From")

            if from_header:
                display_name, email, domain = parse_sender(from_header)
                if not should_skip(email, domain) and email not in candidates:
                    candidates[email] = (display_name, domain)

            # Also mine forwarded-email headers inside snippet
            for fwd_name, fwd_email in extract_forwarded_contacts(snippet):
                fwd_domain = fwd_email.split("@")[-1]
                if not should_skip(fwd_email, fwd_domain) and fwd_email not in candidates:
                    candidates[fwd_email] = (fwd_name, fwd_domain)

        processed_ids.add(thread_id)

    log.info("Unique candidate contacts: %d", len(candidates))

    results = []
    for email, (name, domain) in candidates.items():
        try:
            existing = find_contact_by_email(email)
            if existing:
                props = build_contact_properties(name, email, domain, existing=existing)
                if props and not dry_run:
                    update_contact(existing["id"], props)
                    status = "Aggiornato"
                elif props:
                    status = "Aggiornato (dry-run)"
                else:
                    status = "Ignorato (nessuna modifica)"
                contact_id = existing["id"]
            else:
                if not dry_run:
                    created = create_contact(name, email, domain)
                    contact_id = created["id"]
                    status = "Creato"
                else:
                    contact_id = "N/A"
                    status = "Creato (dry-run)"

            results.append({"status": status, "email": email, "hubspot_id": contact_id})
            log.info("[%s] %s (ID: %s)", status, email, contact_id)
            time.sleep(0.1)  # gentle rate-limiting

        except Exception as exc:
            log.error("Error processing %s: %s", email, exc)
            results.append({"status": "Errore", "email": email, "hubspot_id": None, "error": str(exc)})

    # Persist state
    state["processed_thread_ids"] = list(processed_ids)[-5000:]  # cap to 5000
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        save_state(state)

    return results


def print_report(results: list[dict]):
    created = [r for r in results if r["status"].startswith("Creato")]
    updated = [r for r in results if r["status"].startswith("Aggiornato") and "nessuna" not in r["status"]]
    ignored = [r for r in results if "Ignorato" in r["status"]]
    errors = [r for r in results if r["status"] == "Errore"]

    print(f"\n{'═'*60}")
    print(f"  Gmail → HubSpot Sync — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═'*60}")
    print(f"  Creati:    {len(created)}")
    print(f"  Aggiornati:{len(updated)}")
    print(f"  Ignorati:  {len(ignored)}")
    print(f"  Errori:    {len(errors)}")
    print(f"{'─'*60}")
    for r in results:
        print(f"  [{r['status']:<30}] {r['email']:<40} ID: {r['hubspot_id']}")
    print(f"{'═'*60}\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--days", type=int, default=GMAIL_LOOKBACK_DAYS,
                        help="How many days back to scan (default: 7)")
    args = parser.parse_args()

    if args.days != GMAIL_LOOKBACK_DAYS:
        GMAIL_LOOKBACK_DAYS = args.days

    results = sync_once(dry_run=args.dry_run)
    print_report(results)
