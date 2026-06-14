"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and upserts sender contacts into HubSpot.

Required env vars:
  GMAIL_CREDENTIALS_FILE  path to OAuth2 credentials JSON (or use ADC)
  HUBSPOT_ACCESS_TOKEN    private-app token with contacts read/write
  LOOKBACK_DAYS           how many days back to scan (default: 1)

Run:
  python gmail_hubspot_sync.py
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── constants ────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"
CONTACT_SOURCE_LABEL = "Inbound Gmail"

SKIP_DOMAINS = {
    "facebookmail.com",
    "googlemail.com",
    "google.com",
    "bounce.notifications.google.com",
    "mailer-daemon",
}
SKIP_PREFIXES = ("mailer-daemon", "noreply", "no-reply", "notification@")

# ── Gmail helpers ────────────────────────────────────────────────────────────


def _gmail_service():
    creds_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = "token.json"
    creds: Optional[Credentials] = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_senders(lookback_days: int = 1) -> list[dict]:
    """Return deduplicated list of {email, name, subject, date} for inbound senders."""
    service = _gmail_service()
    after = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y/%m/%d"
    )
    query = f"in:inbox -from:me newer_than:{lookback_days}d"

    results = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=500)
        .execute()
    )
    messages = results.get("messages", [])
    seen: dict[str, dict] = {}

    for msg_stub in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_stub["id"], format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        name, email = parseaddr(raw_from)
        email = email.lower().strip()

        if not email or _should_skip(email):
            continue

        if email not in seen:
            seen[email] = {
                "email": email,
                "name": name.strip() or None,
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            }

    return list(seen.values())


def _should_skip(email: str) -> bool:
    domain = email.split("@")[-1] if "@" in email else ""
    if domain in SKIP_DOMAINS:
        return True
    for prefix in SKIP_PREFIXES:
        if email.startswith(prefix):
            return True
    return False


# ── HubSpot helpers ──────────────────────────────────────────────────────────


def _hs_headers() -> dict:
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _hs_get(path: str, params: dict = None) -> dict:
    r = requests.get(f"{HUBSPOT_BASE}{path}", headers=_hs_headers(), params=params)
    r.raise_for_status()
    return r.json()


def _hs_post(path: str, body: dict) -> dict:
    r = requests.post(f"{HUBSPOT_BASE}{path}", headers=_hs_headers(), json=body)
    r.raise_for_status()
    return r.json()


def _hs_patch(path: str, body: dict) -> dict:
    r = requests.patch(f"{HUBSPOT_BASE}{path}", headers=_hs_headers(), json=body)
    r.raise_for_status()
    return r.json()


def find_contact(email: str) -> Optional[dict]:
    """Return existing HubSpot contact or None."""
    body = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
        "limit": 1,
    }
    data = _hs_post("/crm/v3/objects/contacts/search", body)
    results = data.get("results", [])
    return results[0] if results else None


def _company_from_domain(email: str) -> Optional[str]:
    """Best-effort company name from email domain."""
    domain = email.split("@")[-1] if "@" in email else ""
    if not domain or domain in {"gmail.com", "yahoo.com", "hotmail.com", "libero.it",
                                 "outlook.com", "icloud.com", "tiscali.it"}:
        return None
    # Remove TLD and capitalise, e.g. visitlmr.it → Visitlmr
    name = domain.split(".")[0].replace("-", " ").replace("_", " ").title()
    return name


def _parse_name(sender_name: str, email: str) -> tuple[Optional[str], Optional[str]]:
    """Return (firstname, lastname) best-effort."""
    if not sender_name:
        # Try to infer from email local part, e.g. e.boffa → Boffa
        local = email.split("@")[0]
        parts = re.split(r"[._\-]", local)
        if len(parts) >= 2 and all(p.isalpha() for p in parts):
            return parts[0].capitalize(), parts[-1].capitalize()
        return None, None

    parts = sender_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], None


def create_contact(sender: dict) -> dict:
    firstname, lastname = _parse_name(sender.get("name") or "", sender["email"])
    company = _company_from_domain(sender["email"])

    props: dict[str, str] = {
        "email": sender["email"],
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    data = _hs_post("/crm/v3/objects/contacts", {"properties": props})
    _add_note(data["id"], sender)
    return data


def update_contact(contact: dict, sender: dict) -> dict:
    cid = contact["id"]
    existing = contact["properties"]
    updates: dict[str, str] = {}

    if not existing.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"

    if not existing.get("firstname") or not existing.get("lastname"):
        fn, ln = _parse_name(sender.get("name") or "", sender["email"])
        if fn and not existing.get("firstname"):
            updates["firstname"] = fn
        if ln and not existing.get("lastname"):
            updates["lastname"] = ln

    if not existing.get("company"):
        company = _company_from_domain(sender["email"])
        if company:
            updates["company"] = company

    if updates:
        _hs_patch(f"/crm/v3/objects/contacts/{cid}", {"properties": updates})

    _add_note(cid, sender)
    return contact


def _add_note(contact_id: str, sender: dict) -> None:
    body_text = (
        f"Email ricevuta via Gmail\n"
        f"Oggetto: {sender.get('subject', '')}\n"
        f"Data: {sender.get('date', '')}\n"
        f"Fonte: {CONTACT_SOURCE_LABEL}"
    )
    note_body = {
        "properties": {
            "hs_note_body": body_text,
            "hs_timestamp": str(int(time.time() * 1000)),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202}],
            }
        ],
    }
    try:
        _hs_post("/crm/v3/objects/notes", note_body)
    except Exception as exc:
        log.warning("Note creation failed for %s: %s", contact_id, exc)


# ── main loop ────────────────────────────────────────────────────────────────


def sync_once(lookback_days: int = 1) -> list[dict]:
    senders = fetch_inbox_senders(lookback_days)
    log.info("Found %d unique inbound senders", len(senders))
    results = []

    for sender in senders:
        email = sender["email"]
        try:
            existing = find_contact(email)
            if existing:
                update_contact(existing, sender)
                status = "AGGIORNATO"
                contact_id = existing["id"]
            else:
                created = create_contact(sender)
                status = "CREATO"
                contact_id = created["id"]

            entry = {"status": status, "email": email, "hubspot_id": contact_id}
            results.append(entry)
            log.info("[%s] %s (id=%s)", status, email, contact_id)

        except Exception as exc:
            entry = {"status": "ERRORE", "email": email, "error": str(exc)}
            results.append(entry)
            log.error("[ERRORE] %s — %s", email, exc)

    return results


def main():
    lookback = int(os.environ.get("LOOKBACK_DAYS", "1"))
    results = sync_once(lookback)
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
