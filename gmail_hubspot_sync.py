#!/usr/bin/env python3
"""Gmail → HubSpot Contact Sync

Monitors Gmail inbox continuously, extracts sender contacts (including from
forwarded emails), then creates or updates them in HubSpot without duplicates.

Usage:
    python gmail_hubspot_sync.py            # continuous loop (default 5 min)
    python gmail_hubspot_sync.py --once     # single pass
    python gmail_hubspot_sync.py --interval 60
"""

import argparse
import email.utils
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
STATE_FILE = Path(os.getenv("SYNC_STATE_FILE", "sync_state.json"))
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
HUBSPOT_API = "https://api.hubapi.com"

# Emails/domains to always skip
SKIP_DOMAINS = {
    "facebookmail.com",
    "accounts.google.com",
    "googlemail.com",
    "mailer-daemon.googlemail.com",
    "bounce.google.com",
}
SKIP_PREFIXES = {"no-reply", "noreply", "mailer-daemon", "postmaster", "notification"}

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"processed_ids": [], "last_epoch": None}


def save_state(state: dict) -> None:
    state["processed_ids"] = list(state["processed_ids"])[-10_000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Gmail auth ────────────────────────────────────────────────────────────────

def get_gmail_service():
    if not CREDENTIALS_FILE.exists():
        sys.exit(
            f"[ERRORE] File credenziali Gmail non trovato: {CREDENTIALS_FILE}\n"
            "Scarica OAuth2 credentials da Google Cloud Console e salvalo come credentials.json"
        )
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)

# ── Email parsing ──────────────────────────────────────────────────────────────

def _should_skip(addr: str) -> bool:
    addr = addr.lower()
    domain = addr.split("@")[-1] if "@" in addr else ""
    prefix = addr.split("@")[0] if "@" in addr else addr
    return domain in SKIP_DOMAINS or prefix in SKIP_PREFIXES


def _parse_name(raw_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname)."""
    parts = raw_name.strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _company_from_domain(addr: str) -> str:
    """Derive a human-readable company name from the email domain."""
    domain = addr.split("@")[-1]
    # Strip TLD and split
    name = re.sub(r"\.(it|com|org|net|eu|io|ch|uk|de|fr)$", "", domain)
    name = re.sub(r"\.(comune|consiglio|regione|provincia)\.", " ", name)
    parts = [p.capitalize() for p in re.split(r"[\.\-_]", name) if p]
    return " ".join(parts)


def extract_sender_from_headers(headers: dict) -> Optional[tuple[str, str, str]]:
    """(email, firstname, lastname) from the From: header, or None to skip."""
    from_val = headers.get("From", "")
    name, addr = email.utils.parseaddr(from_val)
    addr = addr.lower().strip()
    if not addr or "@" not in addr or _should_skip(addr):
        return None
    fn, ln = _parse_name(name) if name else ("", "")
    return addr, fn, ln


# Patterns for Italian-style forwarded mails (Fw:/Fwd:)
_FWD_PATTERNS = [
    # Da "Nome Cognome" email@example.com
    re.compile(r'Da\s+"([^"]{2,80})"\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'),
    # Da Nome Cognome <email@example.com>
    re.compile(r'Da:\s*([^<\n]{2,60}?)\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>'),
    # From: "Name" <email>
    re.compile(r'From:\s*"?([^<"\n]{2,60}?)"?\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>'),
    # Da email@example.com (no name)
    re.compile(r'Da\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'),
]


def extract_sender_from_snippet(snippet: str) -> Optional[tuple[str, str, str]]:
    """Try to find the original sender in a forwarded-email snippet."""
    for pattern in _FWD_PATTERNS:
        m = pattern.search(snippet)
        if not m:
            continue
        if m.lastindex == 2:
            name_raw, addr = m.group(1).strip(), m.group(2).lower().strip()
        else:
            name_raw, addr = "", m.group(1).lower().strip()
        if not addr or "@" not in addr or _should_skip(addr):
            continue
        fn, ln = _parse_name(name_raw) if name_raw else ("", "")
        return addr, fn, ln
    return None

# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_TOKEN}", "Content-Type": "application/json"}


def hs_find_contact(addr: str) -> Optional[dict]:
    """Return the HubSpot contact dict or None."""
    url = f"{HUBSPOT_API}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": addr}]}
        ],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    r = requests.post(url, json=body, headers=_hs_headers(), timeout=10)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(addr: str, firstname: str, lastname: str, company: str) -> dict:
    props: dict = {"email": addr, "hs_lead_status": "NEW"}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    r = requests.post(
        f"{HUBSPOT_API}/crm/v3/objects/contacts",
        json={"properties": props},
        headers=_hs_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, updates: dict) -> dict:
    r = requests.patch(
        f"{HUBSPOT_API}/crm/v3/objects/contacts/{contact_id}",
        json={"properties": updates},
        headers=_hs_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def hs_add_note(contact_id: str, body: str) -> None:
    """Create a note and associate it with a contact."""
    timestamp_ms = str(int(time.time() * 1000))
    r = requests.post(
        f"{HUBSPOT_API}/crm/v3/objects/notes",
        json={"properties": {"hs_note_body": body, "hs_timestamp": timestamp_ms}},
        headers=_hs_headers(),
        timeout=10,
    )
    r.raise_for_status()
    note_id = r.json()["id"]
    # 202 = note → contact association type
    requests.put(
        f"{HUBSPOT_API}/crm/v3/objects/notes/{note_id}/associations/contacts/{contact_id}/202",
        headers=_hs_headers(),
        timeout=10,
    )

# ── Core processing ────────────────────────────────────────────────────────────

def process_message(msg_id: str, snippet: str, from_header: str) -> dict:
    """
    Determine the real sender, look up HubSpot, and create/update the contact.
    Returns a result dict: {status, email, id}.
    """
    # Prefer real sender hidden in forwarded body
    sender = extract_sender_from_snippet(snippet)
    if not sender:
        headers_map = {"From": from_header}
        sender = extract_sender_from_headers(headers_map)
    if not sender:
        return {"status": "Ignorato", "email": "", "id": ""}

    addr, firstname, lastname = sender
    company = _company_from_domain(addr) if not (firstname or lastname) else ""

    existing = hs_find_contact(addr)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if existing:
        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates = {}
        if not props.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not props.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not props.get("company") and company:
            updates["company"] = company
        if updates:
            hs_update_contact(contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Invariato"
        return {"status": status, "email": addr, "id": contact_id}
    else:
        new = hs_create_contact(addr, firstname, lastname, company)
        contact_id = new["id"]
        hs_add_note(
            contact_id,
            f"Fonte contatto: Gmail - Inbound Gmail\n"
            f"Email ricevuta il {today}. Contatto acquisito automaticamente.",
        )
        return {"status": "Creato", "email": addr, "id": contact_id}

# ── Sync loop ─────────────────────────────────────────────────────────────────

def run_once(gmail_svc, state: dict) -> dict:
    """Fetch new inbox messages, process each, return counts."""
    query = "in:inbox -from:me -category:promotions"
    if state.get("last_epoch"):
        query += f" after:{state['last_epoch']}"

    try:
        results = gmail_svc.users().messages().list(
            userId="me", q=query, maxResults=100
        ).execute()
    except HttpError as e:
        print(f"[GMAIL] API error: {e}")
        return {}

    messages = results.get("messages", [])
    processed_ids: set = set(state.get("processed_ids", []))
    counts = {"Creato": 0, "Aggiornato": 0, "Invariato": 0, "Ignorato": 0}

    for ref in messages:
        msg_id = ref["id"]
        if msg_id in processed_ids:
            continue

        try:
            msg = gmail_svc.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
        except HttpError as e:
            print(f"  [WARN] Could not fetch message {msg_id}: {e}")
            processed_ids.add(msg_id)
            continue

        snippet = msg.get("snippet", "")
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")

        try:
            result = process_message(msg_id, snippet, from_header)
        except requests.HTTPError as e:
            print(f"  [HUBSPOT ERROR] {e}")
            continue

        status = result["status"]
        counts[status] = counts.get(status, 0) + 1

        if status != "Ignorato":
            print(f"  [{status}] {result['email']}  ID: {result.get('id', '-')}")

        processed_ids.add(msg_id)
        time.sleep(0.15)  # gentle rate limit

    state["processed_ids"] = list(processed_ids)
    state["last_epoch"] = int(time.time())
    return counts


def run_continuous(poll_interval: int = 300) -> None:
    if not HUBSPOT_TOKEN:
        sys.exit("[ERRORE] Imposta la variabile d'ambiente HUBSPOT_TOKEN")

    gmail = get_gmail_service()
    state = load_state()

    print(f"[{_ts()}] Gmail → HubSpot sync avviato (ogni {poll_interval}s)")
    while True:
        try:
            print(f"\n[{_ts()}] Scansione inbox Gmail...")
            counts = run_once(gmail, state)
            save_state(state)
            total = sum(v for k, v in counts.items() if k != "Ignorato")
            print(f"  Riepilogo → {counts}  |  contatti utili: {total}")
            print(f"  Prossimo check tra {poll_interval}s  (Ctrl+C per uscire)")
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\nSync interrotto. Salvataggio stato...")
            save_state(state)
            break
        except Exception as exc:
            print(f"[ERRORE] {exc}")
            time.sleep(30)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitora Gmail e sincronizza i mittenti su HubSpot"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Esegui un solo passaggio e termina"
    )
    parser.add_argument(
        "--interval", type=int, default=300,
        help="Secondi tra un check e l'altro (default: 300)"
    )
    args = parser.parse_args()

    if not HUBSPOT_TOKEN:
        sys.exit("[ERRORE] Imposta la variabile d'ambiente HUBSPOT_TOKEN")

    gmail = get_gmail_service()
    state = load_state()

    if args.once:
        print(f"[{_ts()}] Esecuzione singola...")
        counts = run_once(gmail, state)
        save_state(state)
        print(f"\nRiepilogo: {counts}")
    else:
        run_continuous(args.interval)


if __name__ == "__main__":
    main()
