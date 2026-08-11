"""Response tokens. FR-TOK.

A token is an opaque 256-bit random value (ADR-006). It is not signed and
carries no structure, so it leaks nothing about the donor, the request, or the
system - and it cannot be validated without a datastore lookup, which is the
accepted cost.

Only the SHA-256 hash is stored. A plain hash is sufficient rather than a keyed
one: the token already carries 256 bits of entropy, so there is nothing to
brute force, and a key would add custody burden for no gain (SAD 7.5).

The security property that matters most here is the one that is easy to lose:
an invalid token, an expired token and an already-consumed token must be
INDISTINGUISHABLE. Any difference - a different page, a different status code,
a measurably different response time - is an oracle that tells an attacker
whether a token they guessed was ever real.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import text
from sqlalchemy.orm import Session

from bga.models.requests import Alert, ResponseToken

# 32 bytes of entropy rendered as 43 url-safe characters. Every character costs
# a septet in the SMS, so this is also the single largest item in the message
# budget - see domain/dispatch.py.
_TOKEN_BYTES = 32

_PEEK = text(
    """
    SELECT t.token_hash, t.alert_id
      FROM core.response_token t
     WHERE t.token_hash = :token_hash
       AND t.consumed_at IS NULL
       AND t.expires_at > now()
    """
)

# FR-TOK-04, verbatim. One statement whose affected-row count IS the answer.
# The read-then-write form - check whether it is consumed, then consume it -
# lets a donor tapping twice, or a forwarded link opened by a family member,
# both pass the check and both record a response.
_REDEEM = text(
    """
    UPDATE core.response_token
       SET consumed_at = now()
     WHERE token_hash = :token_hash AND consumed_at IS NULL AND expires_at > now()
    RETURNING alert_id
    """
)


def _hash(raw: str) -> bytes:
    return sha256(raw.encode("utf-8")).digest()


def issue_token(session: Session, alert: Alert, ttl_minutes: int) -> str:
    """Create a token for this alert and return the raw value ONCE.

    The raw value is never persisted and never logged. It exists in memory
    long enough to be rendered into one SMS body, and after that only the
    donor's handset has it.
    """
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    session.add(
        ResponseToken(
            token_hash=_hash(raw),
            alert_id=alert.id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
        )
    )
    session.flush()
    return raw


def peek(session: Session, raw: str) -> Alert | None:
    """Resolve a token to its alert WITHOUT consuming it.

    Used by the GET so that opening the link twice before answering does not
    burn the token. Returns None for invalid, expired and consumed alike.
    """
    if not raw:
        return None

    digest = _hash(raw)
    row = session.execute(_PEEK, {"token_hash": digest}).first()
    if row is None:
        return None

    # The lookup above already matched on the hash; this is the constant-time
    # confirmation. It costs nothing and it means the equality that decides
    # the outcome is never a short-circuiting one.
    if not hmac.compare_digest(bytes(row.token_hash), digest):
        return None

    return session.get(Alert, row.alert_id)


def redeem(session: Session, raw: str) -> Alert | None:
    """Consume the token and return its alert, or None.

    None covers three cases deliberately: the token never existed, it has
    expired, or it has already been used. The caller cannot tell them apart,
    and neither can the person holding the link.
    """
    if not raw:
        return None

    row = session.execute(_REDEEM, {"token_hash": _hash(raw)}).first()
    if row is None:
        return None

    return session.get(Alert, row.alert_id)
