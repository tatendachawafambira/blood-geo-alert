"""The dispatch worker. Drains the outbox and submits to a gateway.

    python -m bga.workers.dispatch_worker

Separate from the API because its failure mode is different: it must survive a
provider outage measured in minutes, and it must not hold a clinician waiting.
Coupling it to request handling would make a gateway outage look like an
application outage (SAD 5.1).

THREE THINGS HERE THAT THE BLOCK SPEC DOES NOT MENTION AND THE SYSTEM NEEDS
--------------------------------------------------------------------------
1. Stale lock reclamation. The drain commits state='LOCKED' so other workers
   skip the row. If the process then dies - which acceptance requirement 6
   asks you to do on purpose - those rows stay LOCKED forever and their
   messages are never sent. Nothing else in the design recovers them. That is
   what locked_by and locked_at are for, and reclaim_stale_locks is what uses
   them.

2. Facility scope. core.alert has FORCE ROW LEVEL SECURITY. A worker holding
   no scope sees zero alerts and updates zero rows - silently, no error. The
   scope travels in the outbox payload (core.outbox has no RLS) and every
   alert write below happens inside scoped_session.

3. provider_message_id is written back into the outbox payload on success.
   The receipts webhook arrives knowing only that id, and it cannot read
   core.alert to find the facility because of the same RLS. core.outbox is the
   only unscoped table that can resolve it. See api/receipts.py.
"""

from __future__ import annotations

import base64
import random
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import structlog
from sqlalchemy import text

from bga.adapters.base import get_adapter, primary_gateway_name, secondary_gateway_name
from bga.config import settings
from bga.crypto.field import decrypt_msisdn
from bga.db import SessionLocal, scoped_session
from bga.domain import audit

log = structlog.get_logger("bga.dispatch")


# --- SQL -------------------------------------------------------------------

# ADR-013, competing consumers. One statement, not a SELECT then an UPDATE:
# two workers running the naive form both read the same QUEUED rows and both
# submit them. SKIP LOCKED is what makes a second worker take different rows
# instead of blocking behind the first.
_DRAIN = text(
    """
    UPDATE core.outbox o
       SET state = 'LOCKED', locked_by = :worker_id, locked_at = now()
     WHERE o.id IN (
           SELECT id FROM core.outbox
            WHERE state = 'QUEUED' AND next_attempt_at <= now()
            ORDER BY next_attempt_at
            LIMIT :batch_size
              FOR UPDATE SKIP LOCKED
           )
    RETURNING o.id, o.alert_id, o.payload, o.attempts
    """
)

# Rows left LOCKED by a worker that died. Returned to the queue rather than
# retried in place, so the ordinary next_attempt_at path applies.
_RECLAIM = text(
    """
    UPDATE core.outbox
       SET state = 'QUEUED', locked_by = NULL, locked_at = NULL
     WHERE state = 'LOCKED'
       AND locked_at < now() - make_interval(secs => :timeout)
    RETURNING id
    """
)

# The alert and its request, read inside the facility scope. Also the donor's
# current consent state, which is the point of the re-check.
_ALERT_CONTEXT = text(
    """
    SELECT a.id            AS alert_id,
           a.deficit_request_id,
           a.state_rank,
           d.id            AS donor_id,
           d.opt_out_at,
           d.availability
      FROM core.alert a
      JOIN core.donor d ON d.id = a.donor_id
     WHERE a.id = :alert_id
    """
)

# Monotonic (ADR-014). Applied only where it strictly advances the rank, so a
# state that has already moved on is never pulled backwards.
_ADVANCE_ALERT = text(
    """
    UPDATE core.alert
       SET state = :state, state_rank = :rank,
           submitted_at = COALESCE(submitted_at, :submitted_at),
           provider_message_id = COALESCE(:provider_message_id, provider_message_id)
     WHERE id = :alert_id AND state_rank < :rank
    RETURNING id
    """
)

# CAST(:pmid AS text), not a bare parameter: jsonb_build_object accepts "any",
# so Postgres cannot infer the type and rejects the statement with
# "could not determine data type of parameter $1". Written as CAST(...) rather
# than ::text because text() would read the cast colons as a bind parameter.
_MARK_SENT = text(
    """
    UPDATE core.outbox
       SET state = 'SENT', locked_by = NULL, locked_at = NULL, last_error = NULL,
           payload = payload
                     || jsonb_build_object('provider_message_id', CAST(:pmid AS text))
     WHERE id = :outbox_id
    """
)

_MARK_SUPPRESSED = text(
    "UPDATE core.outbox SET state = 'SUPPRESSED', locked_by = NULL, locked_at = NULL"
    " WHERE id = :outbox_id"
)

_REQUEUE = text(
    """
    UPDATE core.outbox
       SET state = 'QUEUED', attempts = :attempts, next_attempt_at = :next_attempt_at,
           locked_by = NULL, locked_at = NULL, last_error = :error
     WHERE id = :outbox_id
    """
)

_DEAD_LETTER = text(
    """
    WITH moved AS (
        UPDATE core.outbox
           SET state = 'DEAD', locked_by = NULL, locked_at = NULL,
               attempts = :attempts, last_error = :error
         WHERE id = :outbox_id
        RETURNING id, payload
    )
    INSERT INTO core.dead_letter (id, outbox_id, payload, error, created_at)
    SELECT gen_random_uuid(), id, payload, :error, :now FROM moved
    """
)

# Alert states this worker sets, with their ranks from ADR-014.
_RANKS = {"SENT": 1, "SUPPRESSED": 5, "DEAD": 5}


# --- circuit breaker -------------------------------------------------------


@dataclass
class CircuitBreaker:
    """Bounds the cost of a failing provider, without flapping.

    The success threshold is the hysteresis. Without it, a provider answering
    one request in three would close the breaker on the first success and open
    it again on the next failure, alternating dispatch between two providers -
    which makes the delivery statistics uninterpretable at exactly the moment
    they matter most (SAD 8.4).
    """

    failure_threshold: int = field(default_factory=lambda: settings.breaker_failure_threshold)
    cooldown_seconds: float = field(default_factory=lambda: settings.breaker_cooldown_seconds)
    success_threshold: int = field(default_factory=lambda: settings.breaker_success_threshold)

    state: str = "CLOSED"
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    opened_at: float | None = None

    def gateway_name(self) -> str:
        """Which provider to use right now."""
        if self.state == "OPEN":
            if self._cooldown_elapsed():
                self._transition("HALF_OPEN")
                return primary_gateway_name()  # a probe
            return secondary_gateway_name()
        return primary_gateway_name()

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.state == "HALF_OPEN":
            self.consecutive_successes += 1
            if self.consecutive_successes >= self.success_threshold:
                self._transition("CLOSED")

    def record_failure(self) -> None:
        self.consecutive_successes = 0
        if self.state == "HALF_OPEN":
            # One failure during probing reopens and restarts the cooldown.
            self._transition("OPEN")
            return
        self.consecutive_failures += 1
        if self.state == "CLOSED" and self.consecutive_failures >= self.failure_threshold:
            self._transition("OPEN")

    def _cooldown_elapsed(self) -> bool:
        return self.opened_at is not None and (
            time.monotonic() - self.opened_at >= self.cooldown_seconds
        )

    def _transition(self, new_state: str) -> None:
        previous, self.state = self.state, new_state
        self.consecutive_successes = 0
        if new_state == "OPEN":
            self.opened_at = time.monotonic()
            self.consecutive_failures = 0
        # An operational alert within 60 seconds of failover is QAS-02.
        log.warning(
            "dispatch.breaker",
            previous=previous,
            state=new_state,
            primary=primary_gateway_name(),
            secondary=secondary_gateway_name(),
        )


# --- worker ----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backoff_delay(attempts: int) -> float:
    """Exponential with FULL jitter, per SAD 10.3.

    uniform(0, ceiling) rather than ceiling: without the jitter every message
    that failed together retries together, and a provider that has just come
    back up is knocked over by the retry storm.
    """
    ceiling = min(settings.retry_cap_seconds, settings.retry_base_seconds * 2**attempts)
    return random.uniform(0, ceiling)


class DispatchWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or f"worker-{uuid4().hex[:12]}"
        self.breaker = CircuitBreaker()
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self, *_: object) -> None:
        """Finish the row in hand, then exit. A hard kill is safe too."""
        log.info("dispatch.stopping", worker_id=self.worker_id)
        self._stopping = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        log.info(
            "dispatch.started",
            worker_id=self.worker_id,
            batch_size=settings.dispatch_batch_size,
            primary=primary_gateway_name(),
            secondary=secondary_gateway_name(),
        )
        while not self._stopping:
            try:
                processed = self.tick()
            except Exception:
                log.exception("dispatch.tick.failed", worker_id=self.worker_id)
                processed = 0
            if processed == 0 and not self._stopping:
                time.sleep(settings.worker_poll_seconds)

        log.info("dispatch.stopped", worker_id=self.worker_id)

    def tick(self) -> int:
        self.reclaim_stale_locks()
        rows = self.drain()
        for row in rows:
            if self._stopping:
                # Hand back what we will not process, rather than leaving it
                # LOCKED for the reclaim timeout.
                self._release(row.id)
                continue
            self.process(row)
        return len(rows)

    # -- steps -------------------------------------------------------------

    def reclaim_stale_locks(self) -> int:
        """Return rows abandoned by a dead worker to the queue.

        Without this, killing the worker mid-batch strands every LOCKED row
        permanently and acceptance requirement 6 fails.
        """
        with SessionLocal() as session, session.begin():
            reclaimed = session.execute(
                _RECLAIM, {"timeout": settings.worker_lock_timeout_seconds}
            ).all()
        if reclaimed:
            log.warning("dispatch.reclaimed", count=len(reclaimed), worker_id=self.worker_id)
        return len(reclaimed)

    def drain(self) -> list:
        with SessionLocal() as session, session.begin():
            return session.execute(
                _DRAIN,
                {"worker_id": self.worker_id, "batch_size": settings.dispatch_batch_size},
            ).all()

    def _release(self, outbox_id) -> None:
        with SessionLocal() as session, session.begin():
            session.execute(
                _REQUEUE,
                {
                    "outbox_id": outbox_id,
                    "attempts": 0,
                    "next_attempt_at": _now(),
                    "error": None,
                },
            )

    def process(self, row) -> None:
        payload = row.payload
        facility_id = payload.get("facility_id")
        alert_id = payload["alert_id"]

        if facility_id is None:
            # Pre-5b rows, or a payload built by something that does not know
            # about RLS. Failing loudly beats updating zero alert rows and
            # reporting success.
            log.error("dispatch.payload.no_facility", outbox_id=str(row.id), alert_id=alert_id)
            self._dead_letter(row, "payload carries no facility_id", row.attempts)
            return

        with scoped_session(facility_id=facility_id, actor_id=self.worker_id, role="WORKER") as s:
            context = s.execute(_ALERT_CONTEXT, {"alert_id": alert_id}).first()

            if context is None:
                # RLS returns nothing rather than raising, so this is the shape
                # a scoping mistake takes. It is not "alert deleted".
                log.error(
                    "dispatch.alert.invisible",
                    outbox_id=str(row.id),
                    alert_id=alert_id,
                    facility_id=facility_id,
                    hint="alert missing, or the facility scope does not match",
                )
                self._dead_letter(row, "alert not visible under its facility scope", row.attempts)
                return

            bound = log.bind(
                correlation_id=str(context.deficit_request_id),
                alert_id=alert_id,
                outbox_id=str(row.id),
                worker_id=self.worker_id,
            )

            # 2. SUPPRESSION RE-CHECK (QAS-08). The donor was consented when
            # the cohort was matched; this closes the window between then and
            # now. One indexed read per message, and it is not optional.
            if context.opt_out_at is not None or context.availability != "AVAILABLE":
                self._advance_alert(s, alert_id, "SUPPRESSED", None, None)
                s.execute(_MARK_SUPPRESSED, {"outbox_id": row.id})
                bound.info(
                    "dispatch.suppressed",
                    reason="OPTED_OUT" if context.opt_out_at else "PAUSED",
                )
                audit.emit(
                    action="ALERT_SUPPRESSED",
                    actor=f"worker:{self.worker_id}",
                    target_class="Alert",
                    target_id=alert_id,
                    correlation_id=context.deficit_request_id,
                    detail={"reason": "OPTED_OUT" if context.opt_out_at else "PAUSED"},
                )
                return

            # 3. SUBMIT. The number exists in memory for the length of this
            # call and is never logged, never stored, never returned upwards.
            gateway_name = self.breaker.gateway_name()
            adapter = get_adapter(gateway_name)
            to_e164 = decrypt_msisdn(base64.b64decode(payload["msisdn_ciphertext_b64"]))

            result = adapter.submit(to_e164, payload["body"], payload["idempotency_key"])
            del to_e164

            if result.accepted:
                self.breaker.record_success()
                submitted_at = _now()
                self._advance_alert(
                    s, alert_id, "SENT", result.provider_message_id, submitted_at
                )
                s.execute(
                    _MARK_SENT, {"outbox_id": row.id, "pmid": result.provider_message_id}
                )
                bound.info(
                    "dispatch.sent",
                    gateway=gateway_name,
                    provider_message_id=result.provider_message_id,
                    attempts=row.attempts,
                )
                return

            self.breaker.record_failure()
            attempts = row.attempts + 1

            if result.retriable and attempts < settings.max_attempts:
                delay = backoff_delay(attempts)
                s.execute(
                    _REQUEUE,
                    {
                        "outbox_id": row.id,
                        "attempts": attempts,
                        "next_attempt_at": _now() + timedelta(seconds=delay),
                        "error": result.error,
                    },
                )
                bound.warning(
                    "dispatch.retry",
                    gateway=gateway_name,
                    attempts=attempts,
                    delay_seconds=round(delay, 2),
                    error=result.error,
                )
                return

            # Exhausted, or a failure the provider says will not succeed on a
            # retry. Nothing is ever silently dropped (P-04).
            self._advance_alert(s, alert_id, "DEAD", None, None)
            self._dead_letter(row, result.error or "submission rejected", attempts, session=s)
            bound.error(
                "dispatch.dead_letter",
                gateway=gateway_name,
                attempts=attempts,
                retriable=result.retriable,
                error=result.error,
            )
            audit.emit(
                action="ALERT_DEAD_LETTERED",
                actor=f"worker:{self.worker_id}",
                target_class="Alert",
                target_id=alert_id,
                correlation_id=context.deficit_request_id,
                detail={"attempts": attempts, "gateway": gateway_name},
            )

    # -- helpers -----------------------------------------------------------

    def _advance_alert(self, session, alert_id, state, provider_message_id, submitted_at):
        session.execute(
            _ADVANCE_ALERT,
            {
                "alert_id": alert_id,
                "state": state,
                "rank": _RANKS[state],
                "provider_message_id": provider_message_id,
                "submitted_at": submitted_at,
            },
        )

    def _dead_letter(self, row, error: str, attempts: int, session=None) -> None:
        params = {
            "outbox_id": row.id,
            "error": error,
            "attempts": attempts,
            "now": _now(),
        }
        if session is not None:
            session.execute(_DEAD_LETTER, params)
            return
        with SessionLocal() as own, own.begin():
            own.execute(_DEAD_LETTER, params)


def main() -> None:
    DispatchWorker().run()


if __name__ == "__main__":
    main()
