"""Shared data models (no heavy dependencies)."""

import email.utils


class SenderInfo:
    def __init__(self, raw_from: str, subject: str, message_id: str) -> None:
        self.message_id = message_id
        self.subject = subject

        name, addr = email.utils.parseaddr(raw_from)
        self.email = addr.lower().strip()
        self.name = name.strip() if name else ""
        self.domain = self.email.split("@")[1] if "@" in self.email else ""

    def __repr__(self) -> str:
        return f"SenderInfo(email={self.email!r}, name={self.name!r})"
