#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and automatically syncs sender contacts to HubSpot CRM.

Usage:
    # Continuous monitoring (default: every 60s)
    python gmail_hubspot_sync.py

    # One-shot run
    python gmail_hubspot_sync.py --once

    # Custom poll interval
    python gmail_hubspot_sync.py --interval 120

Requirements:
    HUBSPOT_ACCESS_TOKEN env var must be set (HubSpot Private App token).
    Google OAuth2 credentials.json must be present (downloaded from Google Cloud Console).
"""

import os
import re
import json
import time
import logging
import argparse
from email.utils import parseaddr
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate
from hubspot.crm.contacts.exceptions import ApiException

# ─── Config ─────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL = 60          # seconds between checks
CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"
STATE_FILE = ".gmail_sync_state.json"

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "protonmail.com", "protonmail.ch",
    "libero.it", "virgilio.it", "alice.it", "tiscali.it", "tin.it",
    "fastwebnet.it", "inwind.it",
}

SKIP_SENDERS = {
    "mailer-daemon@googlemail.com",
    "mailer-daemon@yahoo.com",
    "postmaster@",
    "noreply@",
    "no-reply@",
    "notifications@",
    "notification@",
    "analytics-noreply@",
    "do-not-reply@",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Gmail helpers ───────────────────────────────────────────────────────────

def build_gmail(credentials_file: str = "credentials.json",
                token_file: str = "token.json"):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_new_messages(svc, history_id: Optional[str]) -> tuple[list, str]:
    """
    Return (messages, new_history_id).
    On first run uses message list; subsequently uses Gmail History API
    so only truly new messages are returned.
    """
    try:
        if history_id:
            resp = svc.users().history().list(
                userId="me",
                startHistoryId=history_id,
                labelId="INBOX",
                historyTypes=["messageAdded"],
            ).execute()
            msgs = [
                m["message"]
                for rec in resp.get("history", [])
                for m in rec.get("messagesAdded", [])
            ]
            return msgs, resp.get("historyId", history_id)
        else:
            resp = svc.users().messages().list(
                userId="me", labelIds=["INBOX"], maxResults=50
            ).execute()
            profile = svc.users().getProfile(userId="me").execute()
            return resp.get("messages", []), profile.get("historyId", "")
    except Exception as exc:
        log.error("Gmail fetch error: %s", exc)
        return [], history_id or ""


def get_from_header(svc, msg_id: str) -> Optional[str]:
    try:
        msg = svc.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From"],
        ).execute()
        for h in msg.get("payload", {}).get("headers", []):
            if h["name"].lower() == "from":
                return h["value"]
    except Exception as exc:
        log.warning("Could not fetch message %s: %s", msg_id, exc)
    return None


# ─── Parsing helpers ─────────────────────────────────────────────────────────

def should_skip(email: str) -> bool:
    el = email.lower()
    return any(el.startswith(s) or el == s for s in SKIP_SENDERS)


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Returns (display_name, email, domain) or ('', '', '') on failure."""
    name, addr = parseaddr(from_header)
    if not addr or "@" not in addr:
        return "", "", ""
    return name.strip(), addr.lower().strip(), addr.split("@")[1].lower()


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0], "") if parts else ("", "")


def infer_company(domain: str) -> Optional[str]:
    if domain in PERSONAL_DOMAINS:
        return None
    return domain.split(".")[0].replace("-", " ").capitalize()


# ─── HubSpot helpers ─────────────────────────────────────────────────────────

def build_hubspot(access_token: str) -> hubspot.Client:
    return hubspot.Client.create(access_token=access_token)


def find_contact(hs: hubspot.Client, email: str) -> Optional[object]:
    try:
        resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {"filters": [{"propertyName": "email",
                                  "operator": "EQ", "value": email}]}
                ],
                "properties": ["email", "firstname", "lastname",
                                "company", "leadsource"],
                "limit": 1,
            }
        )
        return resp.results[0] if resp.results else None
    except ApiException as exc:
        log.error("HubSpot search error (%s): %s", email, exc)
        return None


def create_contact(hs: hubspot.Client, email: str, first: str,
                   last: str, company: Optional[str]) -> Optional[str]:
    props = {
        "email": email,
        "firstname": first,
        "lastname": last,
        "leadsource": CONTACT_SOURCE,
        "hs_lead_status": INBOUND_TAG,
    }
    if company:
        props["company"] = company
    # Remove empty strings to avoid HubSpot validation errors
    props = {k: v for k, v in props.items() if v}
    try:
        c = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return c.id
    except ApiException as exc:
        log.error("HubSpot create error (%s): %s", email, exc)
        return None


def update_contact(hs: hubspot.Client, contact_id: str,
                   updates: dict) -> bool:
    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input={"properties": updates},
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error (%s): %s", contact_id, exc)
        return False


# ─── Core sync logic ─────────────────────────────────────────────────────────

def process_message(gmail_svc, hs: hubspot.Client, msg_id: str,
                    seen: set) -> dict:
    """
    Process one Gmail message ID.
    Returns: {"status": "Creato"|"Aggiornato"|"Ignorato",
              "email": str, "contact_id": str|None}
    """
    from_hdr = get_from_header(gmail_svc, msg_id)
    if not from_hdr:
        return {"status": "Ignorato", "email": None, "contact_id": None,
                "reason": "no From header"}

    display_name, email, domain = parse_sender(from_hdr)
    if not email:
        return {"status": "Ignorato", "email": None, "contact_id": None,
                "reason": "invalid address"}

    if should_skip(email):
        return {"status": "Ignorato", "email": email, "contact_id": None,
                "reason": "automated sender"}

    if email in seen:
        return {"status": "Ignorato", "email": email, "contact_id": None,
                "reason": "already processed this session"}

    seen.add(email)

    first, last = split_name(display_name) if display_name else ("", "")
    company = infer_company(domain)

    existing = find_contact(hs, email)

    if existing:
        cid = existing.id
        p = existing.properties
        updates: dict = {}
        if not p.get("firstname") and first:
            updates["firstname"] = first
        if not p.get("lastname") and last:
            updates["lastname"] = last
        if not p.get("company") and company:
            updates["company"] = company
        if not p.get("leadsource"):
            updates["leadsource"] = CONTACT_SOURCE
        if updates:
            update_contact(hs, cid, updates)
        return {"status": "Aggiornato", "email": email, "contact_id": cid}

    cid = create_contact(hs, email, first, last, company)
    return {
        "status": "Creato" if cid else "Errore",
        "email": email,
        "contact_id": cid,
    }


# ─── Entry points ─────────────────────────────────────────────────────────────

def run_once(gmail_svc, hs: hubspot.Client) -> list[dict]:
    state = load_state()
    history_id = state.get("history_id")
    seen: set = set()

    messages, new_hid = fetch_new_messages(gmail_svc, history_id)
    log.info("Processing %d messages (history_id: %s → %s)",
             len(messages), history_id, new_hid)

    results = []
    for msg in messages:
        r = process_message(gmail_svc, hs, msg["id"], seen)
        results.append(r)
        if r["email"]:
            log.info("[%s] %-40s | HubSpot ID: %s",
                     r["status"].ljust(10), r["email"],
                     r.get("contact_id") or "—")

    save_state({"history_id": new_hid})
    return results


def run_continuous(gmail_svc, hs: hubspot.Client, interval: int = POLL_INTERVAL):
    log.info("Starting continuous Gmail → HubSpot sync (interval: %ds)", interval)
    while True:
        run_once(gmail_svc, hs)
        log.info("Next check in %ds…", interval)
        time.sleep(interval)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Sync Gmail sender contacts to HubSpot"
    )
    parser.add_argument("--credentials", default="credentials.json",
                        help="Google OAuth2 credentials JSON file")
    parser.add_argument("--token", default="token.json",
                        help="Saved Google OAuth2 token file")
    parser.add_argument("--hubspot-token",
                        default=os.getenv("HUBSPOT_ACCESS_TOKEN"),
                        help="HubSpot Private App access token")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL,
                        help="Poll interval in seconds (default: 60)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single sync pass and exit")
    args = parser.parse_args()

    if not args.hubspot_token:
        parser.error(
            "Set HUBSPOT_ACCESS_TOKEN env var or pass --hubspot-token"
        )

    gmail_svc = build_gmail(args.credentials, args.token)
    hs = build_hubspot(args.hubspot_token)

    if args.once:
        results = run_once(gmail_svc, hs)
        print("\n── Sync Summary ──────────────────────────────────────────")
        for r in results:
            if r["email"]:
                print(f"  [{r['status']:10}] {r['email']:40} | {r.get('contact_id') or '—'}")
        created = sum(1 for r in results if r["status"] == "Creato")
        updated = sum(1 for r in results if r["status"] == "Aggiornato")
        skipped = sum(1 for r in results if r["status"] == "Ignorato")
        print(f"\n  Creati: {created}  |  Aggiornati: {updated}  |  Ignorati: {skipped}")
    else:
        run_continuous(gmail_svc, hs, args.interval)


if __name__ == "__main__":
    main()
