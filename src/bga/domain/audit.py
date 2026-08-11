"""The audit trail. Append-only, hash-chained, written on its own connection.

Three properties make this an audit trail rather than a log table:

  Separate credentials. Rows are written with the bga_audit role over the audit
  engine, never the application session. bga_app holds no grant on the audit
  schema at all, so a compromised application cannot read, alter or delete the
  record of what it did.

  A hash chain. Each row carries sha256(prev_hash || canonical_json(row)).
  Altering a historical row invalidates every hash after it, so tampering is
  detectable even by someone who acquires write access. This is detection, not
  prevention - which is the honest goal against an insider (SAD 10.1.2).

  Independent transaction. See the note on failure handling below.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import text

from bga.db import AuditSessionLocal

log = structlog.get_logger(__name__)

# Serialises chain construction. Reading "the latest entry_hash" and then
# inserting is a read-then-write race: two concurrent emits would both read the
# same prev_hash and fork the chain into two branches, which is exactly the
# shape of damage the chain exists to detect. A transaction-scoped advisory
# lock costs nothing at a few hundred events an hour and releases on commit.
_CHAIN_LOCK_KEY = 8314071001

_LATEST = text("SELECT entry_hash FROM audit.audit_event ORDER BY id DESC LIMIT 1")
_LOCK = text("SELECT pg_advisory_xact_lock(:key)")
_INSERT = text(
    """
    INSERT INTO audit.audit_event
        (actor, action, target_class, target_id, detail, correlation_id,
         created_at, prev_hash, entry_hash)
    VALUES
        (:actor, :action, :target_class, :target_id, :detail, :correlation_id,
         :created_at, :prev_hash, :entry_hash)
    """
)

# Redaction. SAD 10.4 puts this at the writing layer rather than trusting every
# caller to remember, because a control that depends on everyone remembering is
# not a control (P-07).
_FORBIDDEN_KEYS = re.compile(
    r"msisdn|phone|mobile|number|token|cipher|plaintext|secret|pepper|password|key",
    re.IGNORECASE,
)
_MSISDN_SHAPED = re.compile(r"(\+?263\d{9}|\b0\d{9}\b)")
_TOKEN_SHAPED = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")

REDACTED = "[REDACTED]"


def _redact(value: Any, key: str | None = None) -> Any:
    """Strip anything that looks like an MSISDN, a token, or key material.

    Redacts rather than raises. The alternative - refusing to write the row -
    would mean a careless caller silently loses an audit record, which is worse
    than writing a record with one field blanked. The redaction is logged at
    error level so the caller gets fixed.
    """
    if key is not None and _FORBIDDEN_KEYS.search(key):
        log.error("audit.detail.redacted", reason="forbidden key", field=key)
        return REDACTED

    if isinstance(value, dict):
        return {k: _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        log.error("audit.detail.redacted", reason="raw bytes", field=key)
        return REDACTED
    if isinstance(value, str):
        if _MSISDN_SHAPED.search(value):
            log.error("audit.detail.redacted", reason="msisdn-shaped value", field=key)
            return REDACTED
        if _TOKEN_SHAPED.search(value):
            log.error("audit.detail.redacted", reason="token-shaped value", field=key)
            return REDACTED
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def canonical_json(payload: dict) -> bytes:
    """Byte-stable rendering of a row, so the hash is reproducible.

    Sorted keys and no incidental whitespace: two processes hashing the same
    logical row must agree, or verification fails on formatting rather than on
    tampering.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    ).encode("utf-8")


def emit(
    action: str,
    actor: str,
    target_class: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
    correlation_id: UUID | str | None = None,
) -> None:
    """Write one audit row. Never raises into the caller.

    WHY AN AUDIT FAILURE DOES NOT ROLL BACK THE CALLER
    --------------------------------------------------
    This runs on its own connection and its own transaction, so the caller's
    business transaction is untouched by whatever happens here, and a failure
    is swallowed after being logged at error level.

    The alternative - propagate, and let the business transaction roll back -
    couples every safety-critical action to the availability of the audit
    store. A full disk or a permissions mistake in the audit schema would then
    mean a donor cannot opt out, a clinician cannot raise a request for blood,
    and the dispatch worker stops. An observability component would have become
    a single point of failure for the thing it observes.

    The cost is real and worth stating plainly: a lost audit row is NOT
    detectable from the hash chain. The chain simply continues from the last
    row that was written, so a gap leaves no trace in the data. Detection
    depends entirely on the error log and on alerting over it (NFR-MAIN-05),
    which is why the failure is logged with the full event rather than a
    generic message - the log line is the fallback record.

    That trade is deliberate: FR-CON-04 gives a donor an unconditional right to
    withdraw within sixty seconds, and no logging concern outranks it.
    """
    payload = {
        "action": action,
        "actor": actor,
        "target_class": target_class,
        "target_id": str(target_id) if target_id is not None else None,
        "detail": _redact(detail) if detail is not None else None,
        "correlation_id": str(correlation_id) if correlation_id is not None else None,
        "created_at": datetime.now(timezone.utc),
    }

    try:
        with AuditSessionLocal() as session:
            with session.begin():
                session.execute(_LOCK, {"key": _CHAIN_LOCK_KEY})

                prev_hash = session.execute(_LATEST).scalar()
                entry_hash = sha256((prev_hash or b"") + canonical_json(payload)).digest()

                session.execute(
                    _INSERT,
                    {
                        **payload,
                        "detail": json.dumps(payload["detail"], default=_json_default)
                        if payload["detail"] is not None
                        else None,
                        "prev_hash": prev_hash,
                        "entry_hash": entry_hash,
                    },
                )
    except Exception:
        # Loud, with the whole event, because this log line is now the only
        # record that the action happened.
        log.exception(
            "audit.write.failed",
            action=action,
            actor=actor,
            target_class=target_class,
            target_id=str(target_id) if target_id is not None else None,
            correlation_id=str(correlation_id) if correlation_id is not None else None,
        )
