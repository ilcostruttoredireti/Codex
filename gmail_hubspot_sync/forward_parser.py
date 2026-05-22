"""
Parses forwarded email bodies to extract the *original* sender.

Handles the format used by webmail clients like Aruba/Roundcube (Italian):

    Da "Nome Cognome" email@dominio.it
    A  destinatario@...
    Cc ...
    Data ...
    Oggetto ...

Also handles English format:

    From: "Name" <email@domain.com>
    To: ...
"""
from __future__ import annotations

import re

# Italian webmail: Da "Name" email@domain
_IT_FROM_QUOTED = re.compile(
    r'Da\s+"([^"]+)"\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
    re.MULTILINE,
)
# Italian webmail: Da email@domain  (no display name)
_IT_FROM_PLAIN = re.compile(
    r'^Da\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
    re.MULTILINE,
)

# English format: From: "Name" <email>  or  From: email
_EN_FROM = re.compile(
    r'From:\s+"?([^"<\n]*?)"?\s*<([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>',
    re.MULTILINE,
)
_EN_FROM_PLAIN = re.compile(
    r'From:\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
    re.MULTILINE,
)

# Subject prefix patterns indicating a forwarded message
_FW_SUBJECT = re.compile(r'^(Fw|Fwd|FWD|FW)\s*:', re.IGNORECASE)


def is_forwarded(subject: str) -> bool:
    return bool(_FW_SUBJECT.match(subject.strip()))


def extract_forwarded_sender(body: str) -> tuple[str, str] | None:
    """
    Returns (display_name, email) from the body of a forwarded email,
    or None if the original sender cannot be identified.
    """
    if not body:
        return None

    # Italian format with quoted name: Da "Name" email
    m = _IT_FROM_QUOTED.search(body)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()

    # Italian format plain email: Da email@domain
    m = _IT_FROM_PLAIN.search(body)
    if m:
        return "", m.group(1).strip().lower()

    # Try English format with angle brackets
    m = _EN_FROM.search(body)
    if m:
        name = m.group(1).strip()
        email = m.group(2).strip().lower()
        return name, email

    # Plain English format
    m = _EN_FROM_PLAIN.search(body)
    if m:
        return "", m.group(1).strip().lower()

    return None
