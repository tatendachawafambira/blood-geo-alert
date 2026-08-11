"""A simulated SMS gateway.

Not a stub that returns success. It reproduces the two provider behaviours the
architecture actually depends on, so that ADR-010 can be demonstrated without a
contract, a sender ID, or a bill:

  Idempotency. Resubmitting the same key returns the SAME provider_message_id
  rather than accepting a second message. This is what makes at-least-once
  delivery at the transport layer behave as exactly-once at the domain layer.

  Failure. A settable proportion of submissions fail retriably, which is how
  the retry ladder, the circuit breaker and the dead letter queue get exercised
  live rather than described.

Nothing here is persisted. The adapter is a seam, not a store; the outbox is
the durable record.
"""

from __future__ import annotations

import random
import threading
from uuid import uuid4

from pathlib import Path
from tempfile import gettempdir

from bga.adapters.base import SubmitResult
from bga.config import settings

# An explicit in-process override. None means "follow the shared control file".
# Tests assign to this and expect it to win; leaving it None in normal running
# is what lets the gateway console drive the worker in another process.
failure_rate: float | None = None

# The gateway console runs in the API process; submissions happen in the
# dispatch worker process. Setting a module global in one has no effect on the
# other, so the console's failure-rate control would appear to work and change
# nothing. A tiny file both processes read is the whole mechanism.
#
# Deliberately a file and not a table: this is a demonstration affordance for a
# simulated gateway, and putting it in the schema would imply the real adapters
# have a failure switch too.
CONTROL_FILE = Path(gettempdir()) / "bga_mock_failure_rate"


def current_failure_rate() -> float:
    """The rate in force right now.

    Precedence: an explicit in-process assignment, then the shared control
    file, then settings. The in-process value wins so that a test setting it
    is not silently overridden by a control file some earlier demo left behind.
    """
    if failure_rate is not None:
        return failure_rate
    try:
        return max(0.0, min(1.0, float(CONTROL_FILE.read_text())))
    except (OSError, ValueError):
        return settings.mock_failure_rate


def set_failure_rate(value: float) -> float:
    """Set the rate for every process on this machine."""
    value = max(0.0, min(1.0, float(value)))
    CONTROL_FILE.write_text(str(value))
    return value

# Delivery receipt vocabulary this mock understands, mapped to alert states.
_RECEIPT_STATES = {
    "delivered": "DELIVERED",
    "success": "DELIVERED",
    "opened": "OPENED",
    "failed": "DEAD",
    "undelivered": "DEAD",
}


class MockSmsAdapter:
    name = "mock"

    def __init__(self) -> None:
        # idempotency_key -> provider_message_id, for the life of the process.
        self._issued: dict[str, str] = {}
        # The dispatch worker is competing-consumers; two threads submitting
        # the same key at once must not mint two ids.
        self._lock = threading.Lock()

    def submit(self, to_e164: str, body: str, idempotency_key: str) -> SubmitResult:
        # Idempotency is checked BEFORE the failure roll, and that order is the
        # point. A key that has already been accepted must keep returning its
        # original id even at failure_rate 1.0 - otherwise a retry storm could
        # turn an accepted message into a failure and the worker would send it
        # a second time.
        with self._lock:
            existing = self._issued.get(idempotency_key)
        if existing is not None:
            return SubmitResult(
                accepted=True, provider_message_id=existing, error=None, retriable=False
            )

        rate = current_failure_rate()
        if rate > 0 and random.random() < rate:
            # No id is recorded: nothing was accepted, so a retry gets a fresh
            # roll rather than inheriting a phantom message id.
            return SubmitResult(
                accepted=False,
                provider_message_id=None,
                error="simulated gateway failure",
                retriable=True,
            )

        provider_message_id = f"mock-{uuid4()}"
        with self._lock:
            # Re-check under the lock: another thread may have won the race
            # between the read above and here.
            provider_message_id = self._issued.setdefault(
                idempotency_key, provider_message_id
            )

        return SubmitResult(
            accepted=True,
            provider_message_id=provider_message_id,
            error=None,
            retriable=False,
        )

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        """Always true. There is no shared secret to verify against.

        The real adapter must not do this - see africastalking.py. Accepting
        unverified callbacks in production would let anyone mark any alert
        delivered by guessing a provider message id.
        """
        return True

    def parse_receipt(self, payload: dict) -> tuple[str, str]:
        provider_message_id = payload.get("provider_message_id") or payload.get("id")
        raw_status = str(payload.get("status", "")).strip().lower()

        if not provider_message_id:
            raise ValueError("receipt has no provider message id")
        if raw_status not in _RECEIPT_STATES:
            raise ValueError(f"unrecognised receipt status {raw_status!r}")

        return str(provider_message_id), _RECEIPT_STATES[raw_status]
