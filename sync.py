"""Core sync logic: given a sender dict, create/update HubSpot contact."""

from hubspot_client import HubSpotClient


def sync_sender(client: HubSpotClient, sender: dict, log_timeline: bool = True) -> dict:
    """
    Sync a single sender to HubSpot.

    Returns:
        {
            "status":     "created" | "updated" | "ignored",
            "email":      str,
            "contact_id": str | None,
            "reason":     str,          # only when ignored
        }
    """
    email = sender["email"]
    existing = client.find_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        result = client.update_contact(contact_id, sender)
        status = "ignored" if result.get("no_changes") else "updated"
        if log_timeline:
            client.log_email_received(contact_id, sender)
        return {
            "status": status,
            "email": email,
            "contact_id": contact_id,
        }
    else:
        created = client.create_contact(sender)
        contact_id = created["id"]
        if log_timeline:
            client.log_email_received(contact_id, sender)
        return {
            "status": "created",
            "email": email,
            "contact_id": contact_id,
        }
