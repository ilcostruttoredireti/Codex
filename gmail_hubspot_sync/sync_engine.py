"""Core sync logic: for each Gmail sender → upsert HubSpot contact."""

import logging

from contact_parser import build_hubspot_props, merge_props
from models import SenderInfo
from hubspot_client import ContactResult, HubSpotClient

logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, hubspot: HubSpotClient, source: str, tag: str,
                 ignored_domains: list[str], add_timeline: bool = True) -> None:
        self._hs = hubspot
        self._source = source
        self._tag = tag
        self._ignored = set(d.lower() for d in ignored_domains)
        self._add_timeline = add_timeline

    def process(self, sender: SenderInfo) -> ContactResult:
        if sender.domain.lower() in self._ignored:
            logger.info("IGNORED  %s (domain in ignore list)", sender.email)
            return ContactResult("ignored", sender.email, "")

        new_props = build_hubspot_props(sender, self._source, self._tag)
        existing = self._hs.find_contact_by_email(sender.email)

        if existing:
            contact_id = str(existing.id)
            patch = merge_props(existing.properties, new_props)

            if patch:
                ok = self._hs.update_contact(contact_id, patch)
                status = "updated" if ok else "error"
            else:
                status = "skipped"

            if self._add_timeline:
                self._hs.add_timeline_event(contact_id, sender.email, sender.subject)

            result = ContactResult(status, sender.email, contact_id)
            logger.info("%-8s %s  (id=%s)", status.upper(), sender.email, contact_id)
            return result

        # New contact
        contact_id = self._hs.create_contact(new_props)
        if contact_id:
            if self._add_timeline:
                self._hs.add_timeline_event(contact_id, sender.email, sender.subject)
            result = ContactResult("created", sender.email, contact_id)
            logger.info("CREATED  %s  (id=%s)", sender.email, contact_id)
        else:
            result = ContactResult("error", sender.email, "")
            logger.error("ERROR    %s  — could not create contact", sender.email)

        return result
