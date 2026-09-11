"""Signed one-click unsubscribe tokens (RFC 8058) for the bulk newsletter send.

A token embeds a Contact.id, signed with SESSION_SECRET (already the app's
general-purpose signing secret - itsdangerous is already a dependency for
session cookies) so it can't be forged or enumerated, but needs no separate
key of its own and no database lookup to validate before use.
"""
from itsdangerous import BadData, URLSafeSerializer

from . import config

_SALT = "unsubscribe"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(config.SESSION_SECRET, salt=_SALT)


def generate_token(contact_id: int) -> str:
    return _serializer().dumps(contact_id)


def parse_token(token: str) -> int | None:
    """Return the Contact.id the token was issued for, or None if the token
    is missing/tampered/malformed - never raises."""
    try:
        contact_id = _serializer().loads(token)
    except BadData:
        return None
    return contact_id if isinstance(contact_id, int) else None


def unsubscribe_url(contact_id: int) -> str:
    return f"{config.APP_BASE_URL}/unsubscribe/{generate_token(contact_id)}"
