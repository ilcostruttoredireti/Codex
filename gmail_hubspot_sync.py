"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs sender contacts to HubSpot.
Avoids duplicates using email as unique key; updates existing records.

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client hubspot-api-client python-dotenv

Environment variables (.env):
    HUBSPOT_ACCESS_TOKEN   — HubSpot private app token
    GMAIL_CREDENTIALS_PATH — path to OAuth2 credentials JSON (default: credentials.json)
    GMAIL_TOKEN_PATH       — path to cached token file (default: token.json)
    SYNC_LOOKBACK_DAYS     — how many days back to scan on first run (default: 7)
    STATE_FILE             — path to last-processed timestamp file (default: .sync_state)
    SKIP_DOMAINS           — comma-separated domain substrings to skip (e.g. google.com,facebook.com)
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.utils import parseaddr

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Gmail setup ──────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

AUTOMATED_PREFIXES = (
    "no-reply", "noreply", "do-not-reply", "donotreply",
    "mailer", "notifications", "notification", "newsletter",
    "bounce", "postmaster", "daemon", "autoresponder",
    "security", "alert", "alerts", "info-noreply",
    "confirm", "verify", "unsubscribe",
)

AUTOMATED_DOMAINS = {
    "google.com", "googlemail.com", "youtube.com", "facebookmail.com",
    "linkedin.com", "twitter.com", "instagram.com", "tiktok.com",
    "accounts.google.com", "notify.cloudflare.com",
}

SKIP_DOMAIN_FRAGMENTS = [
    s.strip() for s in os.getenv("SKIP_DOMAINS", "").split(",") if s.strip()
]


def _build_gmail_service():
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
    token_path = os.getenv("GMAIL_TOKEN_PATH", "token.json")
    creds = None

    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _is_automated(email: str) -> bool:
    local, domain = email.lower().split("@", 1) if "@" in email else (email, "")
    if any(local.startswith(p) or local == p for p in AUTOMATED_PREFIXES):
        return True
    if domain in AUTOMATED_DOMAINS:
        return True
    if any(frag in domain for frag in SKIP_DOMAIN_FRAGMENTS):
        return True
    return False


def fetch_new_senders(gmail, since_ts: int) -> list[dict]:
    """Return unique sender records from inbox since Unix timestamp `since_ts`."""
    query = f"in:inbox after:{since_ts} -in:draft -in:sent"
    seen = {}
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = gmail.users().threads().list(**kwargs).execute()
        threads = resp.get("threads", [])

        for thread in threads:
            thread_data = gmail.users().threads().get(
                userId="me", id=thread["id"], format="metadata",
                metadataHeaders=["From", "Date"],
            ).execute()
            for msg in thread_data.get("messages", []):
                headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
                raw_from = headers.get("From", "")
                name, email_addr = parseaddr(raw_from)
                email_addr = email_addr.lower().strip()
                if not email_addr or "@" not in email_addr:
                    continue
                if _is_automated(email_addr):
                    continue
                if email_addr not in seen:
                    domain = email_addr.split("@")[1]
                    seen[email_addr] = {
                        "email": email_addr,
                        "raw_name": name.strip(),
                        "domain": domain,
                        "company": _company_from_domain(domain),
                    }

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return list(seen.values())


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (strips TLD, title-cases)."""
    common_generic = {"gmail", "yahoo", "hotmail", "outlook", "icloud", "protonmail"}
    parts = domain.split(".")
    name = parts[-2] if len(parts) >= 2 else parts[0]
    if name in common_generic:
        return ""
    return name.replace("-", " ").title()


# ── HubSpot setup ────────────────────────────────────────────────────────────

def _build_hubspot_client():
    from hubspot import HubSpot
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN not set")
    return HubSpot(access_token=token)


def _search_contact_by_email(hs, email: str):
    from hubspot.crm.contacts import PublicObjectSearchRequest

    req = PublicObjectSearchRequest(
        filter_groups=[{
            "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
        }],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.results:
        return resp.results[0]
    return None


def _parse_name(raw_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Returns (raw_name, '') if unsplittable."""
    parts = raw_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return raw_name, ""


def _add_gmail_note(hs, contact_id: str):
    """Attach a short activity note to record this Gmail touchpoint."""
    from hubspot.crm.engagements.notes import SimplePublicObjectInputForCreate as NoteCreate
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note_body = f"Inbound Gmail – sender detected in inbox on {today}. Source: Gmail"
    try:
        note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(
                properties={"hs_note_body": note_body, "hs_timestamp": datetime.now(timezone.utc).isoformat()},
            )
        )
        hs.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:
        log.debug("Note attachment skipped for %s: %s", contact_id, exc)


def sync_contact(hs, sender: dict) -> dict:
    """
    Ensure sender exists in HubSpot, creating or updating as needed.
    Returns {"status": "created"|"updated"|"ignored", "email": ..., "id": ...}
    """
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
    from hubspot.crm.contacts.exceptions import ApiException

    email = sender["email"]
    existing = _search_contact_by_email(hs, email)
    first, last = _parse_name(sender["raw_name"])

    props_to_set = {}
    if first:
        props_to_set["firstname"] = first
    if last:
        props_to_set["lastname"] = last
    if sender.get("company"):
        props_to_set["company"] = sender["company"]

    if existing:
        contact_id = existing.id
        # Only update fields that are currently empty
        current = existing.properties or {}
        update_props = {k: v for k, v in props_to_set.items() if not current.get(k) and v}

        if update_props:
            try:
                hs.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input=SimplePublicObjectInput(properties=update_props),
                )
                log.info("Updated  %-40s → id=%s  fields=%s", email, contact_id, list(update_props))
                _add_gmail_note(hs, contact_id)
                return {"status": "updated", "email": email, "id": contact_id}
            except ApiException as exc:
                log.warning("Update failed for %s: %s", email, exc)
        return {"status": "ignored", "email": email, "id": contact_id}

    else:
        try:
            created = hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props_to_set
                )
            )
            log.info("Created  %-40s → id=%s", email, created.id)
            _add_gmail_note(hs, created.id)
            return {"status": "created", "email": email, "id": created.id}
        except ApiException as exc:
            log.warning("Create failed for %s: %s", email, exc)
            return {"status": "error", "email": email, "id": None}


# ── State persistence ─────────────────────────────────────────────────────────

STATE_FILE = os.getenv("STATE_FILE", ".sync_state")


def load_last_run() -> int:
    """Return Unix timestamp of last successful run (or lookback default)."""
    lookback = int(os.getenv("SYNC_LOOKBACK_DAYS", "7"))
    default = int((datetime.now(timezone.utc) - timedelta(days=lookback)).timestamp())
    if not Path(STATE_FILE).exists():
        return default
    try:
        data = json.loads(Path(STATE_FILE).read_text())
        return int(data.get("last_run_ts", default))
    except Exception:
        return default


def save_last_run(ts: int):
    Path(STATE_FILE).write_text(json.dumps({"last_run_ts": ts}))


# ── Main ──────────────────────────────────────────────────────────────────────

def run_sync():
    since_ts = load_last_run()
    run_start = int(datetime.now(timezone.utc).timestamp())

    log.info("=== Gmail → HubSpot sync started (since %s) ===",
             datetime.fromtimestamp(since_ts, timezone.utc).isoformat())

    gmail = _build_gmail_service()
    hs = _build_hubspot_client()

    senders = fetch_new_senders(gmail, since_ts)
    log.info("Found %d unique non-automated senders to process", len(senders))

    results = {"created": [], "updated": [], "ignored": [], "error": []}
    for sender in senders:
        result = sync_contact(hs, sender)
        results[result["status"]].append(result)
        time.sleep(0.1)  # gentle rate-limit

    save_last_run(run_start)

    log.info(
        "=== Sync complete: %d created, %d updated, %d ignored, %d errors ===",
        len(results["created"]),
        len(results["updated"]),
        len(results["ignored"]),
        len(results["error"]),
    )
    return results


if __name__ == "__main__":
    run_sync()
