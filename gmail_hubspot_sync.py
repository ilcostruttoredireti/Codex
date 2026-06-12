#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Continuously monitors the Gmail inbox, extracts sender contacts (including
original senders in forwarded/Italian-format messages), and syncs them to
HubSpot CRM — creating new contacts or filling in missing fields on existing ones.

Usage:
    python gmail_hubspot_sync.py             # continuous polling loop
    python gmail_hubspot_sync.py --once      # single run and exit
    python gmail_hubspot_sync.py --days 7    # scan last N days (default 1)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from html import unescape
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "sync_state.json"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

# ── Gmail OAuth scope ──────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ── HubSpot ───────────────────────────────────────────────────────────────────
HUBSPOT_API = "https://api.hubapi.com"
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# ── Configuration ──────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min default
SCAN_DAYS = int(os.getenv("SCAN_DAYS_ON_FIRST_RUN", "1"))

# Addresses that belong to the operator — never sync these
OWN_ADDRESSES: set[str] = set(
    a.strip().lower()
    for a in os.getenv(
        "OWN_ADDRESSES",
        "pubblica.latestata@gmail.com,cristian.mameli.editore@gmail.com,redazione@latestata.it",
    ).split(",")
    if a.strip()
)

# Regex patterns that identify system/automated senders to skip
_SKIP_RE = re.compile(
    r"(mailer-daemon|postmaster|noreply|no-reply|do-not-reply|donotreply"
    r"|bounce[-+@]|unsubscribe@|notification@|notifications@"
    r"|@.*facebookmail\.com"
    r"|analytics-noreply@google\.com"
    r"|@googlemail\.com"
    r"|@bounces\.|@.*amazonaws\.com)",
    re.IGNORECASE,
)

# Domains that belong to free personal email providers → no company extracted
PERSONAL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr",
        "hotmail.com", "hotmail.it", "hotmail.fr", "hotmail.co.uk",
        "outlook.com", "outlook.it", "live.com", "live.it",
        "libero.it", "alice.it", "tiscali.it", "virgilio.it", "tin.it",
        "icloud.com", "me.com", "mac.com", "protonmail.com", "pm.me",
        "fastmail.com", "fastmail.it", "zoho.com", "tutanota.com",
    }
)


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"history_id": None, "processed_ids": [], "last_run_ts": None}


def save_state(state: dict) -> None:
    # Keep processed_ids bounded so the file doesn't grow forever
    state["processed_ids"] = state.get("processed_ids", [])[-2000:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── Gmail auth & service ───────────────────────────────────────────────────────

def get_gmail_service():
    if not CREDENTIALS_FILE.exists():
        sys.exit(
            f"\n[ERROR] {CREDENTIALS_FILE} not found.\n"
            "Download OAuth credentials from Google Cloud Console:\n"
            "  APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop)\n"
            "  Save as credentials.json next to this script.\n"
        )

    creds: Optional[Credentials] = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── Sender parsing helpers ─────────────────────────────────────────────────────

def _split_name(full_name: str) -> tuple[str, str]:
    """Return (firstname, lastname) from a display name string."""
    name = full_name.strip().strip('"').strip("'")
    # Remove role suffixes like " - Alta Badia Brand" or "| Ufficio Stampa"
    name = re.split(r"\s*[-|]\s*", name)[0].strip()
    parts = name.split()
    if not parts:
        return "", ""
    firstname = parts[0].capitalize()
    lastname = " ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else ""
    return firstname, lastname


def parse_rfc2822_sender(header: str) -> tuple[str, str, str]:
    """Parse 'From:' header.  Returns (email, firstname, lastname)."""
    name, email = parseaddr(header)
    email = email.lower().strip()
    firstname, lastname = _split_name(name) if name else ("", "")
    return email, firstname, lastname


def extract_forwarded_sender(snippet: str) -> Optional[tuple[str, str, str]]:
    """
    Extract original sender from forwarded-email snippets.

    Handles:
    - Italian format  : Da "Name" email@domain  (used by redazione@latestata.it)
    - English format  : ---------- Forwarded message ---------\nFrom: Name <email>
    """
    text = unescape(snippet or "")

    # ── Italian Fw format ──────────────────────────────────────────────────────
    # Da "Marco" press@mannuccionline.com
    m = re.search(
        r'\bDa\s+"([^"]+)"\s+([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
        text,
    )
    if m:
        name, email = m.group(1), m.group(2).lower()
        fn, ln = _split_name(name)
        return email, fn, ln

    # Da email@domain  (no display name)
    m = re.search(r'\bDa\s+([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})', text)
    if m:
        return m.group(1).lower(), "", ""

    # ── English Fwd format ────────────────────────────────────────────────────
    # From: Name <email@domain>
    m = re.search(
        r'From:\s+([^<\n]+?)\s+<([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>',
        text,
    )
    if m:
        name, email = m.group(1).strip(), m.group(2).lower()
        fn, ln = _split_name(name)
        return email, fn, ln

    # From: email@domain  (no display name)
    m = re.search(r'From:\s+([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})', text)
    if m:
        return m.group(1).lower(), "", ""

    return None


def company_from_domain(email: str) -> str:
    """Derive a human-readable company name from an email domain."""
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1].lower()
    if domain in PERSONAL_DOMAINS:
        return ""

    # Strip known subdomains that carry no company meaning
    parts = domain.split(".")
    skip_sub = {
        "www", "mail", "smtp", "press", "ufficiostampa", "media",
        "info", "news", "web", "pa", "pec",
    }
    while len(parts) > 2 and parts[0] in skip_sub:
        parts = parts[1:]

    # Registered domain name is second-to-last part
    company_raw = parts[-2] if len(parts) >= 2 else parts[0]

    # Convert kebab-case / snake_case → Title Case words
    company = re.sub(r"[-_]", " ", company_raw)
    company = re.sub(r"([a-z])([A-Z])", r"\1 \2", company)
    return company.strip().title()


def should_skip(email: str) -> bool:
    email_lower = email.lower().strip()
    if not email_lower or "@" not in email_lower:
        return True
    if email_lower in OWN_ADDRESSES:
        return True
    if _SKIP_RE.search(email_lower):
        return True
    return False


# ── HubSpot API calls ─────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    if not HUBSPOT_TOKEN:
        sys.exit(
            "\n[ERROR] HUBSPOT_ACCESS_TOKEN not set.\n"
            "Create a HubSpot Private App with contacts read+write scope\n"
            "and set the token in your .env file.\n"
        )
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


def hs_find_contact(email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    url = f"{HUBSPOT_API}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    resp = requests.post(url, json=body, headers=_hs_headers(), timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    url = f"{HUBSPOT_API}/crm/v3/objects/contacts"
    resp = requests.post(
        url, json={"properties": props}, headers=_hs_headers(), timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    url = f"{HUBSPOT_API}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(
        url, json={"properties": props}, headers=_hs_headers(), timeout=15
    )
    resp.raise_for_status()
    return resp.json()


def hs_create_note(contact_id: str, body_text: str) -> None:
    """Attach an activity note to a contact."""
    url = f"{HUBSPOT_API}/crm/v3/objects/notes"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    note_props = {
        "hs_timestamp": str(now_ms),
        "hs_note_body": body_text,
    }
    resp = requests.post(
        url, json={"properties": note_props}, headers=_hs_headers(), timeout=15
    )
    if not resp.ok:
        log.debug("Note creation failed (non-critical): %s", resp.text)
        return
    note_id = resp.json().get("id")
    if not note_id:
        return
    # Associate note with the contact
    assoc_url = (
        f"{HUBSPOT_API}/crm/v4/objects/notes/{note_id}"
        f"/associations/contacts/{contact_id}/note_to_contact"
    )
    requests.put(assoc_url, headers=_hs_headers(), timeout=10)


# ── Core sync logic ────────────────────────────────────────────────────────────

def sync_contact(
    email: str,
    firstname: str,
    lastname: str,
    subject: str = "",
    received_at: str = "",
) -> dict:
    """
    Upsert a contact in HubSpot.

    Returns:
        {"status": "created"|"updated"|"skipped", "email": ..., "id": ...,
         "reason": ...}
    """
    result_base = {"email": email, "id": None, "reason": ""}

    if should_skip(email):
        return {**result_base, "status": "skipped", "reason": "filtered address"}

    company = company_from_domain(email)
    existing = hs_find_contact(email)

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        updates: dict[str, str] = {}
        if firstname and not (existing_props.get("firstname") or "").strip():
            updates["firstname"] = firstname
        if lastname and not (existing_props.get("lastname") or "").strip():
            updates["lastname"] = lastname
        if company and not (existing_props.get("company") or "").strip():
            updates["company"] = company

        if updates:
            hs_update_contact(contact_id, updates)
            return {**result_base, "status": "updated", "id": contact_id}
        return {
            **result_base,
            "status": "skipped",
            "id": contact_id,
            "reason": "already up-to-date",
        }

    # ── Create new contact ──────────────────────────────────────────────────
    props: dict[str, str] = {
        "email": email,
        "hs_lead_source": "OTHER",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    created = hs_create_contact(props)
    contact_id = created["id"]

    # Attach timeline note
    note_lines = ["📧 Contatto acquisito via Gmail Inbound", "Tag: Inbound Gmail"]
    if subject:
        note_lines.append(f"Oggetto email: {subject[:120]}")
    if received_at:
        note_lines.append(f"Ricevuta: {received_at}")
    hs_create_note(contact_id, "\n".join(note_lines))

    return {**result_base, "status": "created", "id": contact_id}


# ── Gmail polling ──────────────────────────────────────────────────────────────

def _message_fields(service, msg_id: str) -> Optional[dict]:
    """Fetch lightweight message data (metadata + snippet)."""
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        return msg
    except HttpError as e:
        log.warning("Could not fetch message %s: %s", msg_id, e)
        return None


def _extract_contact_from_message(msg: dict) -> Optional[tuple[str, str, str, str, str]]:
    """
    Return (email, firstname, lastname, subject, date) or None if should skip.
    Handles both direct senders and forwarded-message patterns.
    """
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    from_header = headers.get("From", "")
    subject = headers.get("Subject", "")
    date = headers.get("Date", "")
    snippet = msg.get("snippet", "")

    email, firstname, lastname = parse_rfc2822_sender(from_header)

    # If the sender is one of our own forwarding addresses, dig into the snippet
    # to find the original sender of the press release.
    if email.lower() in OWN_ADDRESSES:
        extracted = extract_forwarded_sender(snippet)
        if extracted:
            email, firstname, lastname = extracted
        else:
            return None

    return email, firstname, lastname, subject, date


def poll_inbox(
    service,
    state: dict,
    days_back: int = 1,
) -> list[dict]:
    """
    Fetch new inbox messages and sync their senders to HubSpot.
    Uses Gmail history API when a historyId is stored, otherwise falls back
    to a date-based search query.
    """
    results: list[dict] = []
    processed_ids: set[str] = set(state.get("processed_ids", []))

    history_id: Optional[str] = state.get("history_id")

    # ── Attempt incremental via history API ────────────────────────────────
    msg_ids_to_process: list[str] = []
    new_history_id = history_id

    if history_id:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            new_history_id = resp.get("historyId", history_id)
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    m = added.get("message", {})
                    if "INBOX" in m.get("labelIds", []):
                        msg_ids_to_process.append(m["id"])
            log.info("History API: %d new inbox message(s)", len(msg_ids_to_process))
        except HttpError as e:
            if e.resp.status == 404:
                log.info("History ID expired — falling back to date query")
                history_id = None
            else:
                raise

    # ── Fallback: search by date ───────────────────────────────────────────
    if not history_id:
        query = f"in:inbox newer_than:{days_back}d"
        page_token = None
        while True:
            kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
            if page_token:
                kwargs["pageToken"] = page_token
            resp = service.users().messages().list(**kwargs).execute()
            new_history_id = resp.get("historyId", new_history_id)
            for m in resp.get("messages", []):
                msg_ids_to_process.append(m["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        log.info("Date-query: %d inbox message(s) found", len(msg_ids_to_process))

    # ── Process each message ───────────────────────────────────────────────
    for msg_id in msg_ids_to_process:
        if msg_id in processed_ids:
            continue

        msg = _message_fields(service, msg_id)
        if not msg:
            continue

        contact = _extract_contact_from_message(msg)
        if not contact:
            processed_ids.add(msg_id)
            continue

        email, firstname, lastname, subject, date = contact

        try:
            res = sync_contact(email, firstname, lastname, subject, date)
        except requests.HTTPError as e:
            log.error("HubSpot error for %s: %s", email, e.response.text if e.response else e)
            continue

        res["message_id"] = msg_id
        results.append(res)
        processed_ids.add(msg_id)

        icon = {"created": "✅", "updated": "🔄", "skipped": "⏭ "}.get(res["status"], "?")
        log.info(
            "%s %-10s %s  (HubSpot ID: %s) %s",
            icon,
            res["status"].upper(),
            email,
            res.get("id") or "—",
            res.get("reason") or "",
        )

    # ── Persist state ──────────────────────────────────────────────────────
    state["history_id"] = new_history_id
    state["processed_ids"] = list(processed_ids)
    state["last_run_ts"] = datetime.now(timezone.utc).isoformat()

    return results


# ── Summary output ─────────────────────────────────────────────────────────────

def print_summary(results: list[dict]) -> None:
    if not results:
        print("\nNessuna nuova email elaborata in questo ciclo.\n")
        return

    created = [r for r in results if r["status"] == "created"]
    updated = [r for r in results if r["status"] == "updated"]
    skipped = [r for r in results if r["status"] == "skipped"]

    width = 72
    print(f"\n{'═' * width}")
    print(f"  RIEPILOGO SYNC  Gmail → HubSpot   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * width}")
    print(f"  ✅ Creati      : {len(created)}")
    print(f"  🔄 Aggiornati  : {len(updated)}")
    print(f"  ⏭  Ignorati    : {len(skipped)}")
    print(f"{'─' * width}")
    print(f"  {'Stato':<12} {'Email':<42} {'ID HubSpot'}")
    print(f"{'─' * width}")
    for r in results:
        if r["status"] == "skipped":
            continue
        icon = "✅" if r["status"] == "created" else "🔄"
        print(f"  {icon} {r['status']:<10} {r['email']:<42} {r.get('id') or '—'}")
    print(f"{'═' * width}\n")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument(
        "--days", type=int, default=SCAN_DAYS,
        help="Days to look back on first run (default: %(default)s)"
    )
    args = parser.parse_args()

    service = get_gmail_service()
    state = load_state()

    if args.once:
        results = poll_inbox(service, state, days_back=args.days)
        save_state(state)
        print_summary(results)
        return

    log.info("Avvio sync continuo (polling ogni %ds) — Ctrl+C per fermare", POLL_INTERVAL)
    while True:
        try:
            results = poll_inbox(service, state, days_back=args.days)
            save_state(state)
            print_summary(results)
        except KeyboardInterrupt:
            log.info("Interruzione richiesta. Salvataggio stato e uscita.")
            save_state(state)
            break
        except Exception:
            log.exception("Errore durante il ciclo di sync — riprovo al prossimo ciclo")

        log.info("Prossimo controllo tra %d secondi...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
