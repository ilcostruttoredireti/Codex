#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails, extracts sender contacts (including from
forwarded messages), and syncs them to HubSpot (create or update, no duplicates).

Usage:
    python gmail_hubspot_sync.py            # run one sync pass
    python gmail_hubspot_sync.py --loop 300 # run every 300 seconds

Environment variables:
    GMAIL_CREDENTIALS_PATH  Path to Gmail OAuth2 credentials JSON (default: credentials.json)
    GMAIL_TOKEN_PATH        Path to cached OAuth2 token (default: token.json)
    HUBSPOT_API_KEY         HubSpot Private App access token (required)
    SYNC_STATE_FILE         Path to state file (default: sync_state.json)
    SYNC_LOOKBACK_DAYS      Days to look back on first run (default: 1)
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Dependency check ────────────────────────────────────────────────────────────
try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    print("Missing Google dependencies. Install with:")
    print("  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)

try:
    import hubspot
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
    from hubspot.crm.contacts.exceptions import ApiException as HubSpotApiException
    from hubspot.crm.contacts.models import (
        PublicObjectSearchRequest, Filter, FilterGroup
    )
except ImportError:
    print("Missing HubSpot dependency. Install with:")
    print("  pip install hubspot-api-client")
    sys.exit(1)


# ── Configuration ──────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
GMAIL_TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "token.json")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", "sync_state.json"))
SYNC_LOOKBACK_DAYS = int(os.getenv("SYNC_LOOKBACK_DAYS", "1"))

# Senders that belong to the user / system — never sync these as contacts
SKIP_SENDER_EXACT = {
    "redazione@latestata.it",
    "cristian.mameli.editore@gmail.com",
    "pubblica.latestata@gmail.com",
}

SKIP_SENDER_PATTERNS = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce",
    "notification@", "notifications@",
    "facebookmail.com", "linkedin.com", "twitter.com",
]

# Free/consumer email domains — company name cannot be inferred from these
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "live.com", "live.it",
    "libero.it", "alice.it", "virgilio.it", "tiscali.it",
    "icloud.com", "me.com", "mac.com", "protonmail.com",
}

# ── Regex patterns ──────────────────────────────────────────────────────────────

# Italian webmail forward header: Da "Name" email  or  Da email
RE_IT_FORWARD_NAMED = re.compile(
    r'Da\s+"([^"]+)"\s+([A-Za-z0-9_.+\-]+@[A-Za-z0-9_.\-]+\.[A-Za-z]{2,})',
    re.IGNORECASE,
)
RE_IT_FORWARD_BARE = re.compile(
    r'Da\s+([A-Za-z0-9_.+\-]+@[A-Za-z0-9_.\-]+\.[A-Za-z]{2,})',
    re.IGNORECASE,
)
# Gmail forward header: Da: Name <email>  or  From: Name <email>
RE_GMAIL_FORWARD = re.compile(
    r'(?:Da|From):\s*([^<\n]+?)\s*<([A-Za-z0-9_.+\-]+@[A-Za-z0-9_.\-]+\.[A-Za-z]{2,})>',
    re.IGNORECASE,
)
# Raw email anywhere in body (last-resort fallback)
RE_EMAIL_ANY = re.compile(r'[A-Za-z0-9_.+\-]+@[A-Za-z0-9_.\-]+\.[A-Za-z]{2,}')


# ── Helpers ─────────────────────────────────────────────────────────────────────

def is_skip_sender(email: str) -> bool:
    low = email.lower().strip()
    if low in SKIP_SENDER_EXACT:
        return True
    return any(p in low for p in SKIP_SENDER_PATTERNS)


def domain_to_company(domain: str) -> str:
    """Best-effort company name from email domain."""
    if not domain or domain in GENERIC_DOMAINS:
        return ""
    # Strip TLD(s) and www prefix, then Title-Case
    clean = re.sub(r'\.(com|it|org|net|gov|edu|io|co|uk|eu|info|biz)$', '', domain, flags=re.I)
    clean = re.sub(r'\.(com|it|org|net|gov|edu|io|co|uk|eu|info|biz)$', '', clean, flags=re.I)
    clean = re.sub(r'^www\.', '', clean, flags=re.I)
    return clean.replace("-", " ").replace("_", " ").replace(".", " ").title()


def split_name(raw: str) -> tuple[str, str]:
    """Split 'First Last...' into (firstname, lastname)."""
    parts = raw.strip().strip('"').split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ── State management ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"processed_ids": [], "last_run_epoch": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Gmail ───────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_PATH, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN_PATH).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    return ""


def _extract_text(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        return _decode_part(payload)
    if mime.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _extract_text(part)
            if text:
                return text
    return ""


def fetch_new_messages(service, since_epoch: int | None, processed_ids: set) -> list[dict]:
    """Return parsed message dicts for inbox messages not yet processed."""
    if since_epoch:
        after_ts = since_epoch
    else:
        after_ts = int((datetime.now(timezone.utc) - timedelta(days=SYNC_LOOKBACK_DAYS)).timestamp())

    query = f"in:inbox after:{after_ts}"
    try:
        resp = service.users().messages().list(userId="me", q=query, maxResults=200).execute()
    except HttpError as e:
        print(f"  Gmail list error: {e}")
        return []

    refs = resp.get("messages", [])
    results = []
    for ref in refs:
        msg_id = ref["id"]
        if msg_id in processed_ids:
            continue
        try:
            raw = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
            headers = {h["name"]: h["value"] for h in raw.get("payload", {}).get("headers", [])}
            body = _extract_text(raw.get("payload", {})) or raw.get("snippet", "")
            results.append({
                "id": msg_id,
                "sender": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "body": body,
                "snippet": raw.get("snippet", ""),
            })
        except HttpError as e:
            print(f"  Warning: could not fetch message {msg_id}: {e}")
    return results


# ── Contact extraction ──────────────────────────────────────────────────────────

def extract_contacts(msg: dict) -> list[dict]:
    """Return unique contact dicts extracted from a single Gmail message."""
    seen: dict[str, dict] = {}

    def add(email: str, name: str = ""):
        email = email.strip().lower()
        if not email or is_skip_sender(email) or email in seen:
            return
        domain = email.split("@")[-1] if "@" in email else ""
        fn, ln = split_name(name)
        seen[email] = {
            "email": email,
            "firstname": fn,
            "lastname": ln,
            "domain": domain,
            "company": domain_to_company(domain),
        }

    raw_sender = msg.get("sender", "")
    body = msg.get("body", "") or msg.get("snippet", "")

    # 1. Direct sender (may already be "Name <email>" format)
    m = re.match(r'"?([^"<\n]+?)"?\s*<([^>]+)>', raw_sender)
    if m:
        add(m.group(2), m.group(1))
    elif raw_sender:
        add(raw_sender)

    # 2. Italian webmail forward headers  (Da "Name" email)
    for m in RE_IT_FORWARD_NAMED.finditer(body):
        add(m.group(2), m.group(1))

    # 3. Italian webmail forward headers  (Da email)
    for m in RE_IT_FORWARD_BARE.finditer(body):
        add(m.group(1))

    # 4. Gmail forward headers  (Da: / From: Name <email>)
    for m in RE_GMAIL_FORWARD.finditer(body):
        add(m.group(2), m.group(1))

    return list(seen.values())


# ── HubSpot ─────────────────────────────────────────────────────────────────────

def get_hubspot_client():
    if not HUBSPOT_API_KEY:
        raise ValueError("Set the HUBSPOT_API_KEY environment variable to your HubSpot Private App token.")
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def hs_find_by_email(client, email: str):
    """Return existing HubSpot contact or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[
            Filter(property_name="email", operator="EQ", value=email)
        ])],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        return resp.results[0] if resp.results else None
    except HubSpotApiException as e:
        print(f"    HubSpot search error for {email}: {e}")
        return None


def hs_create(client, contact: dict):
    props = {
        "email": contact["email"],
        "leadsource": "Gmail",
    }
    if contact.get("firstname"):
        props["firstname"] = contact["firstname"]
    if contact.get("lastname"):
        props["lastname"] = contact["lastname"]
    if contact.get("company"):
        props["company"] = contact["company"]
    try:
        return client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
        )
    except HubSpotApiException as e:
        print(f"    HubSpot create error for {contact['email']}: {e}")
        return None


def hs_update_missing_fields(client, contact_id: str, contact: dict, existing) -> bool:
    """Fill only blank fields; never overwrite populated ones. Returns True if updated."""
    ep = existing.properties or {}
    updates = {}

    if not ep.get("leadsource"):
        updates["leadsource"] = "Gmail"
    if not ep.get("firstname") and contact.get("firstname"):
        updates["firstname"] = contact["firstname"]
    if not ep.get("lastname") and contact.get("lastname"):
        updates["lastname"] = contact["lastname"]
    if not ep.get("company") and contact.get("company"):
        updates["company"] = contact["company"]

    if not updates:
        return False
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except HubSpotApiException as e:
        print(f"    HubSpot update error for {contact['email']}: {e}")
        return False


# ── Main sync pass ──────────────────────────────────────────────────────────────

def sync_once(verbose: bool = True) -> list[dict]:
    state = load_state()
    processed_ids: set = set(state.get("processed_ids", []))
    last_epoch: int | None = state.get("last_run_epoch")

    if verbose:
        print(f"\n[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] "
              f"Gmail → HubSpot sync starting")

    gmail = get_gmail_service()
    hs = get_hubspot_client()

    messages = fetch_new_messages(gmail, last_epoch, processed_ids)
    if verbose:
        print(f"  {len(messages)} new message(s) to process")

    # Aggregate unique contacts across all messages
    all_contacts: dict[str, dict] = {}
    for msg in messages:
        for c in extract_contacts(msg):
            if c["email"] not in all_contacts:
                all_contacts[c["email"]] = c

    if verbose:
        print(f"  {len(all_contacts)} unique contact(s) extracted")

    results = []
    for email, contact in all_contacts.items():
        existing = hs_find_by_email(hs, email)
        if existing is None:
            created = hs_create(hs, contact)
            if created:
                status, hs_id = "Creato", created.id
            else:
                status, hs_id = "Errore", None
        else:
            hs_id = existing.id
            updated = hs_update_missing_fields(hs, hs_id, contact, existing)
            status = "Aggiornato" if updated else "Ignorato"

        entry = {"status": status, "email": email, "hubspot_id": hs_id}
        results.append(entry)
        if verbose:
            print(f"  [{status}] {email} (HubSpot ID: {hs_id})")
        time.sleep(0.05)  # gentle rate-limit

    # Persist state
    processed_ids.update(m["id"] for m in messages)
    state["processed_ids"] = list(processed_ids)[-20_000:]
    state["last_run_epoch"] = int(datetime.now(timezone.utc).timestamp())
    save_state(state)

    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    errors  = sum(1 for r in results if r["status"] == "Errore")
    if verbose:
        print(f"  Done → Creato: {created}, Aggiornato: {updated}, "
              f"Ignorato: {ignored}, Errori: {errors}")
    return results


# ── Entry point ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    parser.add_argument(
        "--loop", type=int, metavar="SECONDS",
        help="Run continuously, sleeping SECONDS between passes"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress per-contact output")
    args = parser.parse_args()

    if args.loop:
        print(f"Running in loop mode, interval={args.loop}s. Press Ctrl-C to stop.")
        while True:
            try:
                sync_once(verbose=not args.quiet)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except Exception as e:
                print(f"  Sync error: {e}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        sync_once(verbose=not args.quiet)


if __name__ == "__main__":
    main()
