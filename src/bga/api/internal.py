"""Internal JSON endpoints: the detail page poll, health, and metrics.

Everything returned is an aggregate. The poll endpoint uses the same read model
the page renders, so there is no separate query path to get wrong (ADR-005),
and health and metrics carry counts and timings only - no donor, no facility
name, no message body. They are unauthenticated for now, so that constraint is
the only thing protecting them.

TWO THINGS THESE NUMBERS CANNOT SEE, said here rather than implied:

  core.alert carries FORCE row-level security, so an unscoped count returns
  zero rather than an error. The alert figures below are therefore summed
  across facilities, one scoped session each. core.outbox and core.dead_letter
  have no RLS and are counted directly.

  The circuit breaker lives in the dispatch worker's memory, in another
  process. This endpoint cannot read it and does not pretend to; it reports
  observable degradation from the database instead and says so.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from bga.adapters import mock_sms
from bga.adapters.base import primary_gateway_name, secondary_gateway_name
from bga.api.deps import get_scoped_session
from bga.db import SessionLocal, scoped_session
from bga.domain import readmodel
from bga.models.reference import Facility

log = structlog.get_logger("bga.internal")

router = APIRouter(tags=["internal"])


@router.get("/api/requests/{request_id}/status")
def request_status(
    request_id: UUID, session: Session = Depends(get_scoped_session)
) -> dict:
    """Counters for one request. Polled every two seconds by the detail page."""
    summary = readmodel.request_summary(session, request_id)
    if not summary:
        # Absent, or belonging to another facility and hidden by RLS. The same
        # answer either way.
        raise HTTPException(status_code=404, detail="request not found")

    progress = readmodel.alert_progress(session, request_id)

    return {
        "request_state": summary["state"],
        "counts": progress["counts"],
        "total": progress["total"],
        "units_required": progress["units_required"],
        "confirmed": progress["confirmed"],
        "pledged_label": progress["pledged_label"],
        "ward_aggregates": readmodel.ward_aggregates(session, request_id),
    }


# --- health and metrics ----------------------------------------------------

_OUTBOX_DEPTH = text(
    "SELECT count(*) FROM core.outbox WHERE state IN ('QUEUED', 'LOCKED')"
)
_OUTBOX_BY_STATE = text("SELECT state, count(*) FROM core.outbox GROUP BY state")
_DEAD_LETTERS = text("SELECT count(*) FROM core.dead_letter")
_RETRYING = text("SELECT count(*) FROM core.outbox WHERE attempts > 0 AND state = 'QUEUED'")

# The worker does not heartbeat anywhere, so this is the freshest evidence the
# database holds that it was alive. Split in two because core.outbox carries no
# RLS and core.alert does: reading them in one unscoped statement returns NULL
# for the alert half without any error, which is how this endpoint reported
# "worker never seen" seconds after the worker had sent ten messages.
_LAST_LOCK = text("SELECT max(locked_at) FROM core.outbox")
_LAST_SUBMISSION = text("SELECT max(submitted_at) FROM core.alert")

_ALERTS_BY_STATE = text("SELECT state, count(*) FROM core.alert GROUP BY state")

# Seconds from a request being raised to its first message reaching a gateway,
# one row per request. Also RLS-bound, so it is gathered per facility scope and
# the median is taken over the combined set - a median of per-facility medians
# would not be the median.
# NFR-PERF-02 targets 60 seconds at the 95th percentile.
_SECONDS_TO_FIRST_SUBMISSION = text(
    """
    SELECT EXTRACT(EPOCH FROM (s.first_submitted - dr.created_at)) AS seconds
      FROM core.deficit_request dr
      JOIN (SELECT deficit_request_id, min(submitted_at) AS first_submitted
              FROM core.alert WHERE submitted_at IS NOT NULL
             GROUP BY deficit_request_id) s
        ON s.deficit_request_id = dr.id
    """
)


def _facility_ids() -> list:
    with SessionLocal() as session:
        return list(session.execute(select(Facility.id)).scalars().all())


def _across_facilities(statement):
    """Yield rows of an RLS-protected query, once per facility scope.

    Reading core.alert without a scope returns nothing and raises nothing, so
    every aggregate over it has to be gathered this way or it silently reports
    zero.
    """
    for facility_id in _facility_ids():
        with scoped_session(
            facility_id=facility_id, actor_id="internal", role="OPERATOR"
        ) as session:
            yield from session.execute(statement)


def _sum_across_facilities(statement) -> dict[str, int]:
    totals: dict[str, int] = {}
    for state, count in _across_facilities(statement):
        totals[state] = totals.get(state, 0) + count
    return totals


@router.get("/internal/health")
def health() -> dict:
    """Liveness and the two numbers that say whether dispatch is moving."""
    database = "ok"
    outbox_depth = 0
    worker_last_seen = None

    try:
        with SessionLocal() as session:
            outbox_depth = session.execute(_OUTBOX_DEPTH).scalar() or 0
            candidates = [session.execute(_LAST_LOCK).scalar()]

        # core.alert is RLS-bound; see the note on _LAST_SUBMISSION.
        candidates += [row[0] for row in _across_facilities(_LAST_SUBMISSION)]
        seen = [value for value in candidates if value is not None]
        worker_last_seen = max(seen).isoformat() if seen else None
    except Exception:
        log.exception("internal.health.db_unreachable")
        database = "fail"

    return {
        "status": "ok" if database == "ok" else "degraded",
        "db": database,
        "outbox_depth": outbox_depth,
        "worker_last_seen": worker_last_seen,
        "worker_last_seen_note": "derived from the latest lock or submission; "
        "the worker does not emit a heartbeat",
    }


@router.get("/internal/metrics")
def metrics() -> dict:
    """Counts and timings. No personal data, by construction."""
    import statistics

    with SessionLocal() as session:
        outbox = {state: count for state, count in session.execute(_OUTBOX_BY_STATE)}
        dead_letters = session.execute(_DEAD_LETTERS).scalar() or 0
        retrying = session.execute(_RETRYING).scalar() or 0

    latencies = [
        float(row[0]) for row in _across_facilities(_SECONDS_TO_FIRST_SUBMISSION)
        if row[0] is not None
    ]
    median_seconds = statistics.median(latencies) if latencies else None

    alerts = _sum_across_facilities(_ALERTS_BY_STATE)
    total_alerts = sum(alerts.values())
    delivered = alerts.get("DELIVERED", 0) + alerts.get("OPENED", 0) + alerts.get(
        "CONFIRMED", 0
    ) + alerts.get("DECLINED", 0)
    confirmed = alerts.get("CONFIRMED", 0)

    return {
        "alerts_by_state": alerts,
        "alerts_total": total_alerts,
        "outbox_by_state": outbox,
        "outbox_depth": outbox.get("QUEUED", 0) + outbox.get("LOCKED", 0),
        "dead_letter_count": dead_letters,
        "retrying_now": retrying,
        "circuit_breaker": {
            "state": "unknown",
            "primary": primary_gateway_name(),
            "secondary": secondary_gateway_name(),
            "simulated_failure_rate": mock_sms.current_failure_rate(),
            "note": "the breaker is held in the dispatch worker's memory, in "
            "another process. retrying_now and dead_letter_count are the "
            "observable proxies until the worker publishes its state.",
        },
        "median_seconds_to_first_submission": (
            round(median_seconds, 2) if median_seconds is not None else None
        ),
        "conversion": {
            # Denominator is alerts that demonstrably reached a handset.
            # Reporting against everything queued would flatter a provider
            # outage into a conversion problem.
            "delivered_or_better": delivered,
            "confirmed": confirmed,
            "rate": round(confirmed / delivered, 4) if delivered else None,
        },
    }
