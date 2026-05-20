"""Email filtering logic — no external dependencies."""

_SKIP_LOCAL_PATTERNS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "bounce", "notifications", "newsletter",
    "info", "support", "admin", "postmaster", "root",
    "automated", "automatic", "autoresponder",
}

_SKIP_DOMAINS = {"noreply.com", "no-reply.com"}


def should_skip(email: str) -> bool:
    """Return True for automated/system senders that should not be synced."""
    if not email or "@" not in email:
        return True
    local, domain = email.lower().split("@", 1)
    if domain in _SKIP_DOMAINS:
        return True
    return any(p in local for p in _SKIP_LOCAL_PATTERNS)
