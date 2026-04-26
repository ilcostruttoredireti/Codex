import logging
from typing import Dict, List, Optional, Tuple

from gmail_client import InvalidHistoryIdError

logger = logging.getLogger(__name__)

# Domains that belong to personal email providers — skip company inference
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk",
    "outlook.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "pm.me", "tutanota.com",
    "zoho.com", "mail.com", "fastmail.com", "hey.com",
}

# Local-part prefixes that indicate automated/system senders — skip these
_NOREPLY_PREFIXES = frozenset(
    [
        "noreply", "no-reply", "donotreply", "do-not-reply",
        "mailer-daemon", "postmaster", "bounce", "bounces",
        "daemon", "auto-reply", "auto_reply",
    ]
)

# Subdomain prefixes that should be stripped before deriving company name
_SUBDOMAIN_NOISE = frozenset(["mail", "email", "smtp", "mx", "webmail", "m", "send"])


class ContactProcessor:
    def __init__(self, gmail, hubspot, state):
        self._gmail = gmail
        self._hubspot = hubspot
        self._state = state

    # ── public ────────────────────────────────────────────────────────────────

    def sync_once(self, backfill_days: int = 0) -> List[Dict]:
        """Fetch new messages, sync each sender to HubSpot, return results."""
        messages = self._fetch_messages(backfill_days)

        results: List[Dict] = []
        seen_emails: set = set()

        for msg in messages:
            if self._state.is_processed(msg["id"]):
                continue
            email_addr = msg.get("sender_email", "")
            # Deduplicate within this batch so the same sender is processed once
            if email_addr and email_addr in seen_emails:
                self._state.mark_processed(msg["id"])
                continue
            if email_addr:
                seen_emails.add(email_addr)

            result = self._process(msg)
            results.append(result)
            self._state.mark_processed(msg["id"])

        return results

    # ── private ───────────────────────────────────────────────────────────────

    def _fetch_messages(self, backfill_days: int) -> List[Dict]:
        if backfill_days > 0:
            logger.info("Backfill: fetching last %d day(s)", backfill_days)
            messages = self._gmail.get_recent_messages(days=backfill_days)
            self._state.last_history_id = self._gmail.get_current_history_id()
            return messages

        if not self._state.last_history_id:
            current_id = self._gmail.get_current_history_id()
            self._state.last_history_id = current_id
            logger.info("First run — baseline historyId=%s, processing today's inbox", current_id)
            return self._gmail.get_recent_messages(days=1)

        try:
            messages = self._gmail.get_new_messages(self._state.last_history_id)
        except InvalidHistoryIdError:
            logger.warning("HistoryId expired, falling back to last 1 day")
            messages = self._gmail.get_recent_messages(days=1)

        self._state.last_history_id = self._gmail.get_current_history_id()
        return messages

    def _process(self, msg: Dict) -> Dict:
        email_addr = msg.get("sender_email", "").strip()

        if not email_addr or "@" not in email_addr:
            return {
                "status": "skipped",
                "email": msg.get("sender_raw", ""),
                "contact_id": None,
                "reason": "invalid_email",
            }

        local = email_addr.split("@")[0].lower()
        if local in _NOREPLY_PREFIXES or local.startswith("noreply") or local.startswith("no-reply"):
            return {
                "status": "skipped",
                "email": email_addr,
                "contact_id": None,
                "reason": "noreply",
            }

        info = self._extract_contact_info(msg)
        return self._upsert(info, msg)

    def _extract_contact_info(self, msg: Dict) -> Dict:
        email_addr = msg["sender_email"]
        domain = email_addr.split("@")[1]
        firstname, lastname = _split_name(msg.get("sender_name", ""))
        company: Optional[str] = None
        if domain not in _PERSONAL_DOMAINS:
            company = _domain_to_company(domain)
        return {
            "email": email_addr,
            "firstname": firstname,
            "lastname": lastname,
            "company": company,
        }

    def _upsert(self, info: Dict, msg: Dict) -> Dict:
        email_addr = info["email"]
        try:
            existing = self._hubspot.find_contact_by_email(email_addr)
            if existing:
                updates = _build_updates(existing["properties"], info)
                if updates:
                    self._hubspot.update_contact(existing["id"], updates)
                    status = "updated"
                else:
                    status = "skipped"
                return {"status": status, "email": email_addr, "contact_id": existing["id"]}

            props = _build_create_props(info)
            created = self._hubspot.create_contact(props)
            cid = created["id"]
            subject = msg.get("subject") or "(no subject)"
            self._hubspot.add_note(
                cid,
                f"[Inbound Gmail] {subject}\nFrom: {msg.get('sender_raw', '')}",
            )
            return {"status": "created", "email": email_addr, "contact_id": cid}

        except Exception as exc:
            logger.error("HubSpot sync failed for %s: %s", email_addr, exc)
            return {
                "status": "error",
                "email": email_addr,
                "contact_id": None,
                "error": str(exc),
            }


# ── module-level helpers ──────────────────────────────────────────────────────

def _split_name(raw: str) -> Tuple[str, str]:
    name = raw.strip().strip("\"'")
    if not name:
        return "", ""
    parts = name.split(None, 1)
    return (parts[0], parts[1]) if len(parts) > 1 else (parts[0], "")


def _domain_to_company(domain: str) -> str:
    parts = domain.lower().split(".")
    if parts[0] in _SUBDOMAIN_NOISE:
        parts = parts[1:]
    # Take the SLD (second-level domain) as company name
    name = parts[-2] if len(parts) >= 2 else parts[0]
    return name.capitalize()


def _build_create_props(info: Dict) -> Dict:
    props: Dict[str, str] = {"email": info["email"], "leadsource": "Gmail"}
    if info["firstname"]:
        props["firstname"] = info["firstname"]
    if info["lastname"]:
        props["lastname"] = info["lastname"]
    if info["company"]:
        props["company"] = info["company"]
    return props


def _build_updates(existing: Dict, new: Dict) -> Dict:
    updates: Dict[str, str] = {}
    if not existing.get("firstname") and new["firstname"]:
        updates["firstname"] = new["firstname"]
    if not existing.get("lastname") and new["lastname"]:
        updates["lastname"] = new["lastname"]
    if not existing.get("company") and new["company"]:
        updates["company"] = new["company"]
    if not existing.get("leadsource"):
        updates["leadsource"] = "Gmail"
    return updates
