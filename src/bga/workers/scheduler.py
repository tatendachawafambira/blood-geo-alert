"""Time-driven obligations: escalation, facility notification, expiry.

    python -m bga.workers.scheduler

A single active instance, guarded by a Postgres advisory lock. A second
instance exits cleanly rather than competing, so it can be deployed for
availability without doing the work twice.

Time-triggered work has a failure mode the rest of the system does not: it
fails by NOT HAPPENING, which is invisible unless something watches for it
(ARK-09). Every tick therefore logs, and the liveness signal is the tick
itself.

Every action is idempotent. A scheduler that restarts mid-tick must not widen a
radius twice or notify a facility twice, so each action records that it
happened and checks before repeating.

TWO CONSTRAINTS THE SPEC DOES NOT MENTION
-----------------------------------------
1. Row-level security. core.deficit_request has FORCE RLS, so an unscoped
   scheduler sees no requests at all - silently, because RLS returns nothing
   rather than raising. core.facility has no RLS, so the loop below enumerates
   facilities first and opens a scoped session per facility. At pilot scale
   that is three iterations.

2. There is no notified_at column. DeficitRequest has no field for scheduler
   bookkeeping, and adding one needs a migration this block may not make. The
   flags live under a "scheduler" key inside matched_stats, which is already
   JSONB on the request. A real column would be better and is recorded as
   deferred work.
"""

from __future__ import annotations

import signal
import time
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from bga.config import settings
from bga.db import SessionLocal, scoped_session
from bga.domain import audit
from bga.domain.dispatch import create_alerts_and_outbox
from bga.domain.matching import match_and_reserve
from bga.domain.proximity import proximity_is_stale
from bga.models.reference import Facility
from bga.models.requests import DeficitRequest

log = structlog.get_logger("bga.scheduler")

# Arbitrary but fixed. Any second instance asking for the same key is refused.
LEADER_LOCK_KEY = 8314071002

_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")

_CONFIRMED = text(
    "SELECT count(*) FROM core.alert"
    " WHERE deficit_request_id = :request_id AND state = 'CONFIRMED'"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scheduler_state(request: DeficitRequest) -> dict:
    return dict((request.matched_stats or {}).get("scheduler", {}))


def _record(request: DeficitRequest, **updates) -> None:
    """Persist scheduler bookkeeping onto the request.

    Reassigns matched_stats rather than mutating it: SQLAlchemy does not track
    in-place changes to a JSONB dict, so a mutation would be silently dropped
    and every action would repeat on the next tick.
    """
    stats = dict(request.matched_stats or {})
    stats["scheduler"] = {**stats.get("scheduler", {}), **updates}
    request.matched_stats = stats


class Scheduler:
    def __init__(self) -> None:
        self._stopping = False
        self._lock_connection = None

    # -- leadership --------------------------------------------------------

    def acquire_leadership(self) -> bool:
        """Session-scoped advisory lock, held for the life of the process."""
        self._lock_connection = SessionLocal().connection()
        acquired = bool(self._lock_connection.execute(_TRY_LOCK, {"key": LEADER_LOCK_KEY}).scalar())
        if not acquired:
            self._lock_connection.close()
            self._lock_connection = None
        return acquired

    def request_stop(self, *_: object) -> None:
        log.info("scheduler.stopping")
        self._stopping = True

    # -- the loop ----------------------------------------------------------

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)

        if not self.acquire_leadership():
            log.info(
                "scheduler.not_leader",
                message="another scheduler holds the advisory lock; exiting cleanly",
            )
            return 0

        log.info(
            "scheduler.started",
            tick_seconds=settings.scheduler_tick_seconds,
            no_response_window_minutes=settings.no_response_window_minutes,
            facility_notify_minutes=settings.facility_notify_minutes,
            max_lifetime_minutes=settings.max_request_lifetime_minutes,
        )
        while not self._stopping:
            try:
                self.tick()
            except Exception:
                log.exception("scheduler.tick.failed")
            for _ in range(int(settings.scheduler_tick_seconds * 10)):
                if self._stopping:
                    break
                time.sleep(0.1)

        log.info("scheduler.stopped")
        return 0

    def tick(self) -> dict:
        counts = {"fulfilled": 0, "escalated": 0, "notified": 0, "expired": 0, "seen": 0}

        self.check_proximity_freshness()

        # RLS: enumerate facilities unscoped, then scope per facility.
        with SessionLocal() as session:
            facility_ids = session.execute(select(Facility.id)).scalars().all()

        for facility_id in facility_ids:
            with scoped_session(
                facility_id=facility_id, actor_id="scheduler", role="SCHEDULER"
            ) as session:
                requests = (
                    session.execute(
                        select(DeficitRequest).where(DeficitRequest.state == "ACTIVE")
                    )
                    .scalars()
                    .all()
                )
                for request in requests:
                    counts["seen"] += 1
                    outcome = self.process(session, request)
                    if outcome:
                        counts[outcome] += 1

        log.info("scheduler.tick", **counts)
        return counts

    def check_proximity_freshness(self) -> bool:
        """ARK-08. A stale proximity table does not raise - it silently
        produces the wrong catchment, which is this system's worst failure
        class. Checked every tick precisely because nothing else would notice.
        """
        with SessionLocal() as session:
            stale = proximity_is_stale(session)
        if stale:
            log.error(
                "scheduler.proximity.stale",
                message="an active collection point has no proximity rows; "
                "catchments for it are silently empty. Run rebuild_proximity.",
            )
        return stale

    # -- per request -------------------------------------------------------

    def process(self, session: Session, request: DeficitRequest) -> str | None:
        now = _now()
        age = now - request.created_at
        confirmed = session.execute(_CONFIRMED, {"request_id": request.id}).scalar() or 0
        state = _scheduler_state(request)

        if confirmed >= request.units_required:
            request.state = "FULFILLED"
            request.closed_at = now
            session.flush()
            audit.emit(
                action="REQUEST_FULFILLED",
                actor="scheduler",
                target_class="DeficitRequest",
                target_id=request.id,
                correlation_id=request.id,
                detail={"confirmed": confirmed, "units_required": request.units_required},
            )
            log.info("scheduler.fulfilled", request_id=str(request.id), confirmed=confirmed)
            return "fulfilled"

        if age > timedelta(minutes=settings.max_request_lifetime_minutes):
            request.state = "EXPIRED"
            request.closed_at = now
            session.flush()
            audit.emit(
                action="REQUEST_EXPIRED",
                actor="scheduler",
                target_class="DeficitRequest",
                target_id=request.id,
                correlation_id=request.id,
                detail={"confirmed": confirmed, "age_minutes": int(age.total_seconds() // 60)},
            )
            log.info("scheduler.expired", request_id=str(request.id))
            return "expired"

        if age > timedelta(minutes=settings.no_response_window_minutes):
            if self.widen(session, request, now):
                return "escalated"

        if age > timedelta(minutes=settings.facility_notify_minutes) and not state.get(
            "facility_notified_at"
        ):
            _record(request, facility_notified_at=now.isoformat())
            session.flush()
            # FR-ESC-03: the facility is told so it can pursue alternative
            # sourcing rather than waiting. Never fail silently.
            audit.emit(
                action="FACILITY_NOTIFIED_NO_CONVERSION",
                actor="scheduler",
                target_class="DeficitRequest",
                target_id=request.id,
                correlation_id=request.id,
                detail={"confirmed": confirmed, "units_required": request.units_required},
            )
            log.warning(
                "scheduler.facility_notified",
                request_id=str(request.id),
                confirmed=confirmed,
                units_required=request.units_required,
            )
            return "notified"

        return None

    def widen(self, session: Session, request: DeficitRequest, now: datetime) -> bool:
        """Widen by exactly one band and alert only the newly reachable donors."""
        bands = list(settings.radius_bands)
        current = request.radius_metres or bands[0]
        wider = [band for band in bands if band > current]
        if not wider:
            return False
        next_band = wider[0]

        # match_and_reserve walks settings.radius_bands from the first, and
        # takes no band argument. Restricting the list for the duration of the
        # call is how one band is widened rather than all of them at once. The
        # scheduler is single-threaded and holds the leader lock, so nothing
        # else is reading this value. A band parameter on match_and_reserve
        # would be cleaner and is recorded as deferred work.
        original = settings.radius_bands
        settings.radius_bands = [next_band]
        try:
            # THE DELTA COMES FROM THE FREQUENCY CAP, NOT FROM ARITHMETIC HERE.
            # Donors already alerted for this request have last_alerted_at set,
            # so the reservation's cap predicate excludes them and only newly
            # reachable donors are returned. Do not "optimise" this by
            # subtracting previous cohorts: the cap is what makes it correct
            # under concurrency, and re-alerting a donor who already declined
            # is exactly what destroys a donor register.
            result = match_and_reserve(
                session,
                request.collection_point_id,
                request.abo,
                request.rh,
                request.units_required,
                now,
            )
        finally:
            settings.radius_bands = original

        request.radius_metres = next_band
        escalations = int(_scheduler_state(request).get("escalations", 0)) + 1
        _record(request, escalations=escalations, last_escalated_at=now.isoformat())

        if result.reserved_donors:
            request.state = "ESCALATED"
            session.flush()
            create_alerts_and_outbox(session, request, result.reserved_donors, now)

        # Back to ACTIVE so the next tick reconsiders it.
        request.state = "ACTIVE"
        session.flush()

        audit.emit(
            action="REQUEST_ESCALATED",
            actor="scheduler",
            target_class="DeficitRequest",
            target_id=request.id,
            correlation_id=request.id,
            detail={
                "radius_metres": next_band,
                "newly_reserved": len(result.reserved_donors),
                "escalations": escalations,
            },
        )
        log.info(
            "scheduler.escalated",
            request_id=str(request.id),
            radius_metres=next_band,
            newly_reserved=len(result.reserved_donors),
        )
        return True


def main() -> int:
    return Scheduler().run()


if __name__ == "__main__":
    raise SystemExit(main())
