#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail INBOX and upserts sender contacts into HubSpot CRM.

Usage:
    python sync.py              # single pass (cron-friendly)
    python sync.py --watch      # continuous polling loop
    POLL_INTERVAL_SECONDS=30 python sync.py --watch
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
]
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))

HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
HUBSPOT_API = "https://api.hubapi.com"

STATE_FILE = Path(os.getenv("STATE_FILE", ".sync_state.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Local part prefixes that indicate automated/system senders
_SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
    "notifications", "notification", "automated", "auto-confirm",
    "support", "info@", "admin@",
)


# ─── State helpers ────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": []}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ─── Gmail helpers ────────────────────────────────────────────────────────────

def _get_gmail_service():
    creds: Optional[Credentials] = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _fetch_inbox_ids(service, skip: set[str]) -> list[str]:
    """Return all INBOX message IDs not already in *skip*."""
    ids: list[str] = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "labelIds": ["INBOX"], "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        for m in resp.get("messages", []):
            if m["id"] not in skip:
                ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _get_headers(service, msg_id: str) -> dict[str, str]:
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    return {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }


# ─── Sender parsing ───────────────────────────────────────────────────────────

def _parse_sender(from_header: str) -> Optional[dict]:
    """
    Return a dict with contact fields, or None if the sender should be skipped.
    """
    display, email = parseaddr(from_header)
    email = email.lower().strip()

    if not email or "@" not in email:
        return None

    local, domain = email.split("@", 1)

    # Skip system / automated senders
    if any(email.startswith(p) or local == p.rstrip("@") for p in _SKIP_PREFIXES):
        return None

    # Parse first / last name from display name
    parts = display.strip().split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""

    company = _domain_to_company(domain)

    return {
        "email": email,
        "first_name": first,
        "last_name": last,
        "domain": domain,
        "company": company,
    }


def _domain_to_company(domain: str) -> str:
    """
    Best-effort company name from domain: 'acme.co.uk' → 'Acme'.
    Returns empty string for well-known consumer providers (gmail, yahoo, etc.).
    """
    consumer = {"gmail", "googlemail", "yahoo", "hotmail", "outlook",
                "icloud", "me", "mac", "live", "msn", "protonmail",
                "tutanota", "libero", "virgilio", "tiscali", "fastmail"}
    root = domain.split(".")[0]
    if root in consumer:
        return ""
    return root.capitalize()


# ─── HubSpot client ───────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError(
                "HUBSPOT_TOKEN is not set.\n"
                "Create a Private App in HubSpot Settings → Integrations → Private Apps\n"
                "with scopes: crm.objects.contacts.read, crm.objects.contacts.write,\n"
                "             crm.objects.notes.write, timeline"
            )
        self._s = requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _post(self, path: str, payload: dict, retries: int = 3) -> dict:
        url = f"{HUBSPOT_API}{path}"
        for attempt in range(retries):
            r = self._s.post(url, json=payload)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 10))
                log.warning("HubSpot rate-limit — waiting %ds …", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"HubSpot POST {path} failed after {retries} attempts")

    def _patch(self, path: str, payload: dict) -> dict:
        r = self._s.patch(f"{HUBSPOT_API}{path}", json=payload)
        r.raise_for_status()
        return r.json()

    # ── Contacts ──────────────────────────────────────────────────────────────

    def find_by_email(self, email: str) -> Optional[dict]:
        results = self._post(
            "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [{"filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]}],
                "properties": ["email", "firstname", "lastname", "company"],
                "limit": 1,
            },
        ).get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, props: dict) -> dict:
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})

    # ── Notes / Timeline ──────────────────────────────────────────────────────

    def create_note(self, contact_id: str, body: str) -> None:
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        self._post(
            "/crm/v3/objects/notes",
            {
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": str(ts),
                },
                "associations": [{
                    "to": {"id": contact_id},
                    "types": [{
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note → contact
                    }],
                }],
            },
        )


# ─── Sync logic ───────────────────────────────────────────────────────────────

def _build_props(sender: dict, existing: Optional[dict]) -> dict:
    """Return only the properties that need to be written."""
    ex = (existing or {}).get("properties", {})
    props: dict[str, str] = {}

    def set_if_missing(hs_key: str, value: str) -> None:
        if value and not ex.get(hs_key):
            props[hs_key] = value

    props["email"] = sender["email"]
    set_if_missing("firstname", sender["first_name"])
    set_if_missing("lastname", sender["last_name"])
    set_if_missing("company", sender["company"])
    set_if_missing("lead_source", "Gmail")

    return props


def _sync_sender(hs: HubSpotClient, sender: dict, subject: str) -> tuple[str, str]:
    """
    Upsert *sender* in HubSpot.
    Returns (status, contact_id): status is 'created' | 'updated' | 'skipped'.
    """
    existing = hs.find_by_email(sender["email"])

    if existing:
        contact_id: str = existing["id"]
        props = _build_props(sender, existing)
        props.pop("email", None)  # email is immutable after creation

        if props:
            hs.update_contact(contact_id, props)
            status = "updated"
        else:
            status = "skipped"
    else:
        created = hs.create_contact(_build_props(sender, None))
        contact_id = created["id"]
        status = "created"

    # Timeline note
    note = (
        f"📧 Email ricevuta via Gmail\n"
        f"Oggetto: {subject}\n"
        f"Fonte contatto: Gmail\n"
        f"Tag: Inbound Gmail"
    )
    try:
        hs.create_note(contact_id, note)
    except Exception as exc:
        log.warning("Could not attach note to contact %s: %s", contact_id, exc)

    return status, contact_id


# ─── Orchestration ────────────────────────────────────────────────────────────

_STATUS_LABEL = {
    "created": "✅ CREATO    ",
    "updated": "🔄 AGGIORNATO",
    "skipped": "⏭  IGNORATO  ",
}


def run_pass(gmail, hs: HubSpotClient, state: dict) -> None:
    processed: set[str] = set(state.get("processed_ids", []))
    new_ids = _fetch_inbox_ids(gmail, processed)
    log.info("Nuovi messaggi da processare: %d", len(new_ids))

    for msg_id in new_ids:
        try:
            headers = _get_headers(gmail, msg_id)
            from_hdr = headers.get("From", "")
            subject = headers.get("Subject", "(nessun oggetto)")

            if not from_hdr:
                log.debug("Messaggio %s senza header From — ignorato.", msg_id)
                processed.add(msg_id)
                continue

            sender = _parse_sender(from_hdr)
            if not sender:
                log.info(
                    "⏭  IGNORATO   %-42s  (mittente di sistema)",
                    from_hdr[:42],
                )
                processed.add(msg_id)
                continue

            status, contact_id = _sync_sender(hs, sender, subject)
            log.info(
                "%s  email=%-42s  hubspot_id=%s",
                _STATUS_LABEL[status],
                sender["email"],
                contact_id,
            )

        except HttpError as exc:
            log.error("Gmail API error on message %s: %s", msg_id, exc)
        except requests.HTTPError as exc:
            log.error("HubSpot API error on message %s: %s", msg_id, exc)
        except Exception as exc:
            log.error("Unexpected error on message %s: %s", msg_id, exc, exc_info=True)
        finally:
            processed.add(msg_id)

    # Bound stored IDs to 10 000 to keep the state file small
    state["processed_ids"] = list(processed)[-10_000:]
    _save_state(state)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gmail → HubSpot contact sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables:
  GMAIL_CREDENTIALS_FILE   Path to OAuth2 credentials JSON  (default: credentials.json)
  GMAIL_TOKEN_FILE         Path to cached token              (default: token.json)
  HUBSPOT_TOKEN            HubSpot Private App token         (required)
  STATE_FILE               Path to processed-IDs state file  (default: .sync_state.json)
  POLL_INTERVAL_SECONDS    Seconds between polls in --watch  (default: 60)
""",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll Gmail continuously rather than running once",
    )
    args = parser.parse_args()

    hs = HubSpotClient(HUBSPOT_TOKEN)
    gmail = _get_gmail_service()
    state = _load_state()

    if args.watch:
        log.info("Avvio sync continuo (ogni %ds). Ctrl-C per fermare.", POLL_INTERVAL)
        while True:
            try:
                run_pass(gmail, hs, state)
            except KeyboardInterrupt:
                log.info("Interruzione manuale.")
                break
            except Exception as exc:
                log.error("Errore durante il pass: %s", exc, exc_info=True)
            time.sleep(POLL_INTERVAL)
    else:
        run_pass(gmail, hs, state)


if __name__ == "__main__":
    main()
