#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox continuously, extracts sender info from every new email,
and creates or updates the corresponding contact in HubSpot.

Setup
-----
1. Copy .env.example → .env and fill in HUBSPOT_ACCESS_TOKEN.
2. Place your Google OAuth credentials in credentials.json
   (download from Google Cloud Console → APIs & Services → Credentials).
3. Run once interactively to complete the OAuth flow:
       python setup_gmail_oauth.py
4. Then start the service:
       python gmail_hubspot_sync.py
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))          # seconds
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "7"))           # how far back to scan
MAX_PER_CYCLE = int(os.getenv("MAX_PER_CYCLE", "50"))          # messages per poll

# Domains that belong to free/personal email providers (no company inference)
FREE_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "icloud.com",
    "me.com", "aol.com", "libero.it", "tiscali.it", "virgilio.it",
    "alice.it", "tin.it", "email.it", "fastwebnet.it", "protonmail.com",
    "proton.me", "tutanota.com", "gmx.com", "gmx.net",
}

# Automated / system senders to skip entirely
_SKIP_RE = re.compile(
    r"(noreply|no-reply|donotreply|do-not-reply|mailer-daemon|postmaster|"
    r"bounce|bounces|notification|notifications|automated|auto-reply|"
    r"autoresponder|newsletter|unsubscribe|support-ticket)",
    re.IGNORECASE,
)


# ─── Gmail ────────────────────────────────────────────────────────────────────


def get_gmail_service():
    """Return an authenticated Gmail API service, refreshing / creating the token."""
    creds = None
    token_path = Path("token.json")
    creds_file = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_file.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found: {creds_file}\n"
                    "Download from Google Cloud Console → APIs & Services → Credentials\n"
                    "then run: python setup_gmail_oauth.py"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_new_message_ids(service, processed_ids: set) -> list[str]:
    """Return message IDs in the inbox that haven't been processed yet."""
    try:
        resp = service.users().messages().list(
            userId="me",
            q=f"in:inbox newer_than:{LOOKBACK_DAYS}d",
            maxResults=MAX_PER_CYCLE,
        ).execute()
    except HttpError as exc:
        print(f"  [Gmail] API error: {exc}")
        return []

    return [m["id"] for m in resp.get("messages", []) if m["id"] not in processed_ids]


def get_message_headers(service, msg_id: str) -> dict | None:
    """Fetch only the From/Subject/Date headers for a message."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except HttpError:
        return None
    return {h["name"]: h["value"] for h in msg["payload"]["headers"]}


# ─── Sender parsing ───────────────────────────────────────────────────────────


def parse_sender(from_header: str) -> dict | None:
    """
    Parse a raw From header into structured sender data.
    Returns None for automated senders or unparseable addresses.
    """
    from_header = from_header.strip()

    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_header)
    if match:
        full_name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        full_name = ""
        email = from_header.lower()

    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return None

    if _SKIP_RE.search(email):
        return None

    name_parts = full_name.split(" ", 1) if full_name else []
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    domain = email.split("@")[1]
    company = (
        domain.split(".")[0].capitalize()
        if domain not in FREE_EMAIL_DOMAINS
        else ""
    )

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
        "domain": domain,
        "company": company,
    }


# ─── HubSpot client ───────────────────────────────────────────────────────────


class HubSpotClient:
    """Thin wrapper around the HubSpot v3 REST API."""

    BASE = "https://api.hubapi.com"

    def __init__(self, token: str):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, **kw) -> dict:
        r = self._session.get(f"{self.BASE}{path}", **kw)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, data: dict) -> dict:
        r = self._session.post(f"{self.BASE}{path}", json=data)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, data: dict) -> dict:
        r = self._session.patch(f"{self.BASE}{path}", json=data)
        r.raise_for_status()
        return r.json()

    # ── Contacts ──────────────────────────────────────────────────────────────

    def find_contact(self, email: str) -> dict | None:
        """Search for a contact by email. Returns the first match or None."""
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}
            ]}],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        }
        result = self._post("/crm/v3/objects/contacts/search", body)
        return result["results"][0] if result.get("total", 0) > 0 else None

    def create_contact(self, properties: dict) -> dict:
        return self._post("/crm/v3/objects/contacts", {"properties": properties})

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        return self._patch(
            f"/crm/v3/objects/contacts/{contact_id}",
            {"properties": properties},
        )

    # ── Notes / activity ──────────────────────────────────────────────────────

    def add_note(self, contact_id: str, body: str) -> dict | None:
        """Create a note and associate it with a contact."""
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(ts_ms),
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 202,   # note → contact
                }],
            }],
        }
        try:
            return self._post("/crm/v3/objects/notes", payload)
        except requests.HTTPError:
            return None


# ─── Sync logic ───────────────────────────────────────────────────────────────


def sync_contact(hs: HubSpotClient, sender: dict) -> tuple[str, str]:
    """
    Upsert a HubSpot contact from a parsed sender.
    Returns (status, contact_id) where status ∈ {Creato, Aggiornato, Ignorato}.
    """
    existing = hs.find_contact(sender["email"])

    if existing is None:
        props: dict = {
            "email": sender["email"],
            "leadsource": "Gmail",
        }
        if sender["first_name"]:
            props["firstname"] = sender["first_name"]
        if sender["last_name"]:
            props["lastname"] = sender["last_name"]
        if sender["company"]:
            props["company"] = sender["company"]

        contact = hs.create_contact(props)
        contact_id: str = contact["id"]
        hs.add_note(
            contact_id,
            "📧 Email inbound ricevuta via Gmail  |  Tag: Inbound Gmail",
        )
        return "Creato", contact_id

    # Contact exists — fill only empty fields
    contact_id = existing["id"]
    ep = existing.get("properties", {})
    updates: dict = {}

    if not ep.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not ep.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not ep.get("company") and sender["company"]:
        updates["company"] = sender["company"]

    if updates:
        hs.update_contact(contact_id, updates)
        return "Aggiornato", contact_id

    return "Ignorato", contact_id


# ─── State persistence ────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": []}


def save_state(state: dict):
    # Cap at 10 000 IDs to avoid unbounded growth
    state["processed_ids"] = list(state["processed_ids"])[-10_000:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Output ───────────────────────────────────────────────────────────────────

_STATUS_EMOJI = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭ ", "Errore": "❌"}


def print_result_table(rows: list):
    if not rows:
        return
    col_email = max((len(r["email"]) for r in rows), default=5)
    col_email = max(col_email, 5)
    fmt = f"  {{:<2}} {{:<10}} {{:<{col_email}}}  {{}}"
    print(fmt.format("", "STATO", "EMAIL", "ID HUBSPOT"))
    print(f"  {'─' * (col_email + 28)}")
    for r in rows:
        emoji = _STATUS_EMOJI.get(r["stato"], "  ")
        extra = r.get("errore", r.get("id_hubspot", ""))
        print(fmt.format(emoji, r["stato"], r["email"], extra or ""))
    print()


# ─── Main loop ────────────────────────────────────────────────────────────────


def run_cycle(gmail, hs: HubSpotClient, state: dict) -> list[dict]:
    processed: set = set(state["processed_ids"])
    new_ids = fetch_new_message_ids(gmail, processed)
    results: list[dict] = []

    for msg_id in new_ids:
        row: dict = {"msg_id": msg_id, "email": "?", "stato": "Errore"}

        try:
            headers = get_message_headers(gmail, msg_id)
            if not headers:
                processed.add(msg_id)
                continue

            sender = parse_sender(headers.get("From", ""))
            if not sender:
                processed.add(msg_id)
                continue

            row["email"] = sender["email"]
            status, contact_id = sync_contact(hs, sender)
            row["stato"] = status
            row["id_hubspot"] = contact_id

        except requests.HTTPError as exc:
            row["errore"] = f"HTTP {exc.response.status_code}: {exc.response.text[:120]}"
        except Exception as exc:
            row["errore"] = str(exc)
        finally:
            processed.add(msg_id)
            results.append(row)

    state["processed_ids"] = list(processed)
    return results


def main():
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        raise SystemExit(
            "HUBSPOT_ACCESS_TOKEN non trovato.\n"
            "Copia .env.example → .env e compila il token."
        )

    print("=" * 62)
    print("  Gmail → HubSpot Contact Sync")
    print(f"  Polling ogni {POLL_INTERVAL}s  |  Ctrl+C per fermare")
    print("=" * 62 + "\n")

    gmail = get_gmail_service()
    hs = HubSpotClient(hubspot_token)

    while True:
        state = load_state()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        try:
            print(f"[{ts}] Controllo nuove email...", end=" ", flush=True)
            results = run_cycle(gmail, hs, state)

            counts: dict = {}
            for r in results:
                counts[r["stato"]] = counts.get(r["stato"], 0) + 1

            if counts:
                summary = ", ".join(f"{v} {k.lower()}" for k, v in counts.items())
                print(summary)
                print_result_table(results)
            else:
                print("nessuna nuova email.")

        except Exception as exc:
            print(f"\n  Errore ciclo: {exc}")
        finally:
            save_state(state)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
