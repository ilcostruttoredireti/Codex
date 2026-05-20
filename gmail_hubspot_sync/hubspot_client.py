import time
import logging
import requests

logger = logging.getLogger(__name__)

# Email domains that belong to individuals, not companies
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com",
    "hotmail.com", "hotmail.it", "hotmail.co.uk",
    "outlook.com", "outlook.it", "live.com", "live.it", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me",
    "mail.com", "inbox.com", "zohomail.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it", "tim.it",
}


def _extract_company(email: str) -> str | None:
    """Derive a capitalised company name from the email domain, or None."""
    if "@" not in email:
        return None
    domain = email.split("@", 1)[1].lower()
    if domain in _PERSONAL_DOMAINS:
        return None
    # strip TLD(s): acme.co.uk → acme,  mycompany.com → mycompany
    parts = domain.split(".")
    name = parts[0] if len(parts) >= 2 else domain
    return name.replace("-", " ").title()


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


class HubSpotClient:
    BASE = "https://api.hubapi.com"

    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ── internal helper ───────────────────────────────────────────────────────

    def _req(self, method: str, path: str, **kwargs):
        resp = requests.request(method, self.BASE + path, headers=self._headers, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ── public API ────────────────────────────────────────────────────────────

    def find_contact(self, email: str) -> dict | None:
        """Return first matching HubSpot contact or None."""
        try:
            data = self._req("POST", "/crm/v3/objects/contacts/search", json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": email,
                    }]
                }],
                "properties": ["email", "firstname", "lastname", "company", "leadsource"],
            })
            results = data.get("results", [])
            return results[0] if results else None
        except requests.HTTPError as exc:
            logger.error(f"HubSpot search error per {email}: {exc}")
            return None

    def create_contact(self, properties: dict) -> dict:
        return self._req("POST", "/crm/v3/objects/contacts", json={"properties": properties})

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        return self._req("PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": properties})

    def create_note(self, contact_id: str, body: str) -> None:
        """Attach a note to a contact (best-effort; errors are logged, not raised)."""
        try:
            self._req("POST", "/crm/v3/objects/notes", json={
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": str(int(time.time() * 1000)),
                },
                "associations": [{
                    "to": {"id": contact_id},
                    "types": [{
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,   # note → contact
                    }],
                }],
            })
        except Exception as exc:
            logger.warning(f"Impossibile creare nota per contatto {contact_id}: {exc}")

    # ── sync logic ────────────────────────────────────────────────────────────

    def sync_sender(self, email: str, name: str, subject: str, date: str) -> dict:
        """
        Create or update the HubSpot contact for this sender.

        Returns:
            {"status": "CREATO"|"AGGIORNATO"|"IGNORATO", "email": ..., "hubspot_id": ...}
        """
        first, last = _split_name(name)
        company = _extract_company(email)

        existing = self.find_contact(email)

        if existing:
            contact_id = existing["id"]
            props = existing.get("properties", {})

            updates: dict[str, str] = {}
            if first and not props.get("firstname"):
                updates["firstname"] = first
            if last and not props.get("lastname"):
                updates["lastname"] = last
            if company and not props.get("company"):
                updates["company"] = company
            if not props.get("leadsource"):
                updates["leadsource"] = "Gmail"

            if updates:
                self.update_contact(contact_id, updates)
                status = "AGGIORNATO"
            else:
                status = "IGNORATO"

        else:
            new_props: dict[str, str] = {
                "email": email,
                "leadsource": "Gmail",
                "hs_analytics_source": "OTHER_CAMPAIGNS",
            }
            if first:
                new_props["firstname"] = first
            if last:
                new_props["lastname"] = last
            if company:
                new_props["company"] = company

            created = self.create_contact(new_props)
            contact_id = created["id"]
            status = "CREATO"

        # Timeline note (always, regardless of create/update/ignore)
        note = (
            f"Email ricevuta via Gmail\n"
            f"Oggetto: {subject}\n"
            f"Data: {date}\n"
            f"Tag: Inbound Gmail"
        )
        self.create_note(contact_id, note)

        return {"status": status, "email": email, "hubspot_id": contact_id}
