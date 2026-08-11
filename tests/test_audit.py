"""The audit trail: hash chain, redaction, and failure behaviour.

These tests run as the owner role against the test database, so they prove the
chain and the redaction. They cannot prove the PERMISSION posture - that
bga_app holds no grant on the audit schema - because the owner is a superuser.
That property is verified in psql and is acceptance requirement 11.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import text

from bga.domain import audit


def verify_chain(rows) -> tuple[bool, str]:
    """Recompute every entry_hash from the stored row and check the links."""
    previous = None
    for row in rows:
        payload = {
            "action": row.action,
            "actor": row.actor,
            "target_class": row.target_class,
            "target_id": row.target_id,
            "detail": row.detail,
            "correlation_id": str(row.correlation_id) if row.correlation_id else None,
            "created_at": row.created_at.astimezone(timezone.utc),
        }
        expected = sha256((previous or b"") + audit.canonical_json(payload)).digest()
        if row.prev_hash != previous:
            return False, f"row {row.id} does not link to the row before it"
        if row.entry_hash != expected:
            return False, f"row {row.id} contents do not match its entry_hash"
        previous = row.entry_hash
    return True, f"{len(rows)} rows verified"


# --- writing ---------------------------------------------------------------


def test_emit_writes_exactly_one_row(audit_sink):
    audit.emit(action="REQUEST_RAISED", actor="clinician:1")

    rows = audit_sink()
    assert len(rows) == 1
    assert rows[0].action == "REQUEST_RAISED"
    assert rows[0].actor == "clinician:1"


def test_emit_records_every_field_it_is_given(audit_sink):
    correlation = uuid4()
    audit.emit(
        action="ALERTS_QUEUED",
        actor="system",
        target_class="DeficitRequest",
        target_id="req-1",
        detail={"count": 12},
        correlation_id=correlation,
    )

    row = audit_sink()[0]
    assert row.target_class == "DeficitRequest"
    assert row.target_id == "req-1"
    assert row.detail == {"count": 12}
    assert row.correlation_id == correlation


def test_optional_fields_default_to_null(audit_sink):
    audit.emit(action="TICK", actor="scheduler")

    row = audit_sink()[0]
    assert row.target_class is None and row.target_id is None
    assert row.detail is None and row.correlation_id is None


# --- the hash chain --------------------------------------------------------


def test_the_first_row_has_no_predecessor(audit_sink):
    audit.emit(action="FIRST", actor="system")
    assert audit_sink()[0].prev_hash is None


def test_each_row_links_to_the_one_before_it(audit_sink):
    for i in range(5):
        audit.emit(action=f"EVENT_{i}", actor="system")

    rows = audit_sink()
    assert len(rows) == 5
    for earlier, later in zip(rows, rows[1:]):
        assert later.prev_hash == earlier.entry_hash


def test_the_chain_verifies_against_recomputed_hashes(audit_sink):
    audit.emit(action="A", actor="system", detail={"n": 1})
    audit.emit(action="B", actor="system", detail={"n": 2})

    ok, message = verify_chain(audit_sink())
    assert ok, message


def test_editing_a_historical_row_breaks_verification(audit_sink, test_engine):
    """Tamper evidence. Detection, not prevention - which is the honest goal
    against someone who has acquired write access (SAD 10.1.2)."""
    audit.emit(action="A", actor="system")
    audit.emit(action="B", actor="system")

    with test_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE audit.audit_event SET actor = 'mallory'"
                " WHERE id = (SELECT min(id) FROM audit.audit_event)"
            )
        )

    ok, message = verify_chain(audit_sink())
    assert ok is False
    assert "do not match" in message


def test_deleting_a_row_breaks_the_links(audit_sink, test_engine):
    for i in range(3):
        audit.emit(action=f"E{i}", actor="system")

    with test_engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM audit.audit_event"
                " WHERE id = (SELECT min(id) FROM audit.audit_event)"
            )
        )

    ok, _ = verify_chain(audit_sink())
    assert ok is False


def test_concurrent_emits_do_not_fork_the_chain(audit_sink):
    """Reading the latest entry_hash and then inserting is a read-then-write
    race. Without the advisory lock two writers share a prev_hash and the
    chain forks into two branches - the exact damage it exists to detect."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: audit.emit(action="CONCURRENT", actor=f"w{i}"), range(40)))

    rows = audit_sink()
    assert len(rows) == 40
    assert len({r.prev_hash for r in rows}) == 40, "a repeated prev_hash is a fork"

    ok, message = verify_chain(rows)
    assert ok, message


def test_canonical_json_is_key_order_independent():
    a = audit.canonical_json({"b": 2, "a": 1})
    b = audit.canonical_json({"a": 1, "b": 2})
    assert a == b == b'{"a":1,"b":2}'


# --- redaction (SAD 10.4) --------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [
        {"msisdn": "+263771234567"},
        {"phone_number": "0771234567"},
        {"raw_token": "abc"},
        {"msisdn_ciphertext": "abc"},
        {"api_key": "abc"},
        {"blind_pepper": "abc"},
    ],
)
def test_forbidden_key_names_are_redacted(audit_sink, detail):
    audit.emit(action="X", actor="dev", detail=detail)

    stored = audit_sink()[0].detail
    assert list(stored.values()) == [audit.REDACTED]


@pytest.mark.parametrize(
    "value",
    ["+263771234567", "263771234567", "0771234567", "called +263772234567 twice"],
)
def test_msisdn_shaped_values_are_redacted_whatever_the_key_is_called(audit_sink, value):
    audit.emit(action="X", actor="dev", detail={"note": value})

    assert audit_sink()[0].detail["note"] == audit.REDACTED


def test_token_shaped_values_are_redacted(audit_sink):
    audit.emit(action="X", actor="dev", detail={"note": "Xy9_" + "a" * 40})

    assert audit_sink()[0].detail["note"] == audit.REDACTED


def test_raw_bytes_are_redacted(audit_sink):
    audit.emit(action="X", actor="dev", detail={"blob": b"\x01\x02\x03"})

    assert audit_sink()[0].detail["blob"] == audit.REDACTED


def test_nested_structures_are_redacted(audit_sink):
    audit.emit(
        action="X",
        actor="dev",
        detail={"donors": [{"msisdn": "+263771234567", "ward": "Mbare"}]},
    )

    stored = audit_sink()[0].detail
    assert stored["donors"][0]["msisdn"] == audit.REDACTED
    assert stored["donors"][0]["ward"] == "Mbare"


def test_safe_values_survive_untouched(audit_sink):
    audit.emit(
        action="X",
        actor="dev",
        detail={"units": 4, "urgency": "URGENT", "ward": "Mbare", "escalated": True},
    )

    assert audit_sink()[0].detail == {
        "units": 4,
        "urgency": "URGENT",
        "ward": "Mbare",
        "escalated": True,
    }


def test_no_msisdn_digits_survive_a_careless_caller(audit_sink):
    audit.emit(
        action="X",
        actor="dev",
        detail={"msisdn": "+263771234567", "note": "rang +263772234567", "safe": "ok"},
    )

    assert "263" not in str(audit_sink()[0].detail)


# --- failure behaviour -----------------------------------------------------


def test_emit_never_raises_into_the_caller(audit_sink, monkeypatch):
    """An audit outage must not become a system outage.

    If this propagated, a full disk in the audit schema would mean a donor
    cannot opt out - FR-CON-04 gives them that right unconditionally.
    """

    def broken():
        raise RuntimeError("audit store is unreachable")

    monkeypatch.setattr("bga.domain.audit.AuditSessionLocal", broken)

    audit.emit(action="SHOULD_NOT_RAISE", actor="system")  # must not raise


def test_a_failed_emit_writes_nothing(audit_sink, monkeypatch):
    def broken():
        raise RuntimeError("audit store is unreachable")

    monkeypatch.setattr("bga.domain.audit.AuditSessionLocal", broken)
    audit.emit(action="LOST", actor="system")

    assert audit_sink() == []
