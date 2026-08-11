"""Donor matching with atomic contact reservation. ADR-015.

This is the choke point. Every dispatch path routes through here, which is what
makes FR-ELG-07 structurally true rather than aspirationally true: there is no
other way to produce a cohort.

The shape of the reservation statement is the whole point of this module. The
obvious implementation - SELECT the candidates, filter them in Python, then
UPDATE last_alerted_at - is wrong in a way that testing will not show you. Two
requests over overlapping catchments both read, both filter, and both alert the
same people. In a demo nobody notices. In production a donor gets two alerts in
one minute from two hospitals, and that is the experience that makes people opt
out permanently.

One statement instead. The UPDATE re-checks the cap predicate at write time,
and FOR UPDATE ... SKIP LOCKED makes concurrent requests take disjoint sets
rather than queueing behind each other. The cohort is exactly what the UPDATE
returned - never what the diagnostic SELECT found. Under concurrency the two
genuinely differ, and it is the reserved count the clinician must be shown.

There is no spatial operator anywhere below. Proximity was precomputed by
PostGIS into core.ward_collection_proximity (ADR-011); at runtime this is an
indexed integer join, and the composite primary key
(collection_point_id, metres, ward_id) is what turns the radius filter into a
range scan before any donor row is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from bga.config import settings

# Eligibility dates are calendar dates in Zimbabwe, not UTC dates. Between
# 22:00 and midnight UTC the two disagree, and a donor would be judged against
# yesterday.
_HARARE = ZoneInfo("Africa/Harare")

# Exclusion reasons reported to the dashboard. These mirror the predicates in
# the reservation statement exactly, in the same precedence order, so that
# verified_in_radius == eligible + sum(excluded.values()) always holds.
SELF_DECLARED = "SELF_DECLARED"
OPTED_OUT = "OPTED_OUT"
PAUSED = "PAUSED"
UNDER_18 = "UNDER_18"
DEFERRED = "DEFERRED"
INTERVAL_NOT_ELAPSED = "INTERVAL_NOT_ELAPSED"
CONTACTED_RECENTLY = "CONTACTED_RECENTLY"
ELIGIBLE = "ELIGIBLE"


# The reservation. Used exactly as specified - not split into a SELECT and an
# UPDATE, not simplified. Every clause is load-bearing:
#
#   FOR UPDATE OF d2 SKIP LOCKED  - concurrent requests take disjoint sets
#                                   instead of blocking on each other
#   the outer AND (d.last_alerted_at ...) - deliberate belt and braces; the cap
#                                   is re-checked at the moment of the write
#   ORDER BY p.metres             - nearest donors first, then LIMIT applies
#   RETURNING                     - this, and only this, is the cohort
#
# THE CTE IS NOT DECORATION. The specified form of this statement was
#
#     UPDATE core.donor d SET ... WHERE d.id IN (SELECT ... LIMIT n
#                                                FOR UPDATE OF d2 SKIP LOCKED)
#
# and it does not bound the update to n rows. EXPLAIN shows PostgreSQL is free
# to plan the sublink as the INNER side of a nested-loop semi join, which
# re-executes it once per outer donor row. Each execution honours LIMIT n, but
# SKIP LOCKED makes it skip the rows the previous execution just locked, so
# every pass returns a DIFFERENT n rows and the semi join accumulates far more
# than the cap. Measured: 12 donors reserved against a cap of 10, in a single
# band, with no escalation.
#
# It is plan-dependent, which is worse than being wrong. With other statistics
# the planner hashes the sublink, evaluates it once, and the cap appears to
# hold - so the defect is invisible in testing and breaks FR-GEO-07 in
# production, where the register is large enough to change the plan.
#
# AS MATERIALIZED is an optimisation fence: the cohort is computed exactly
# once, and the rows it locked are the rows that get updated. Every clause
# below is otherwise unchanged.
_RESERVE = text(
    """
    WITH cohort AS MATERIALIZED (
           SELECT d2.id
             FROM core.donor d2
             JOIN core.ward_collection_proximity p ON p.ward_id = d2.ward_id
             JOIN core.donor_eligibility e ON e.donor_id = d2.id
            WHERE p.collection_point_id = :cp_id
              AND p.metres <= :radius
              AND d2.abo = :abo AND d2.rh = :rh
              AND d2.verification_state = 'NBSZ_VERIFIED'
              AND d2.availability = 'AVAILABLE'
              AND d2.opt_out_at IS NULL
              AND d2.age_band = 'EIGHTEEN_PLUS'
              AND e.deferral_flag = false
              AND e.next_eligible_date <= :today
              AND (d2.last_alerted_at IS NULL
                   OR d2.last_alerted_at < :cap_cutoff)
            ORDER BY p.metres
            LIMIT :dispatch_cap
              FOR UPDATE OF d2 SKIP LOCKED
         )
    UPDATE core.donor d
       SET last_alerted_at = :now
     WHERE d.id IN (SELECT id FROM cohort)
       AND (d.last_alerted_at IS NULL OR d.last_alerted_at < :cap_cutoff)
    RETURNING d.id, d.preferred_language, d.msisdn_ciphertext
    """
)

# Diagnostics for the dashboard. A plain SELECT that takes no locks and writes
# nothing. It does NOT choose the cohort and its numbers may differ from the
# reservation's under concurrency - that difference is real and is the reason
# the interface reports "reserved", not "matched".
#
# The CASE assigns each donor to the FIRST reason that applies, in the same
# order the reservation's predicates would reject them, so the buckets are
# mutually exclusive and sum to the population in radius.
#
# Both joins are inner, exactly as in the reservation: a donor with no
# eligibility row is absent from both, and so cannot be counted as eligible.
_DIAGNOSTICS = text(
    """
    SELECT CASE
             WHEN d.verification_state <> 'NBSZ_VERIFIED' THEN 'SELF_DECLARED'
             WHEN d.opt_out_at IS NOT NULL                THEN 'OPTED_OUT'
             WHEN d.availability <> 'AVAILABLE'           THEN 'PAUSED'
             WHEN d.age_band <> 'EIGHTEEN_PLUS'           THEN 'UNDER_18'
             WHEN e.deferral_flag                         THEN 'DEFERRED'
             WHEN e.next_eligible_date > :today           THEN 'INTERVAL_NOT_ELAPSED'
             WHEN d.last_alerted_at IS NOT NULL
                  AND d.last_alerted_at >= :cap_cutoff    THEN 'CONTACTED_RECENTLY'
             ELSE 'ELIGIBLE'
           END AS bucket,
           count(*) AS n
      FROM core.donor d
      JOIN core.ward_collection_proximity p ON p.ward_id = d.ward_id
      JOIN core.donor_eligibility e ON e.donor_id = d.id
     WHERE p.collection_point_id = :cp_id
       AND p.metres <= :radius
       AND d.abo = :abo AND d.rh = :rh
     GROUP BY 1
    """
)


@dataclass(frozen=True)
class ReservedDonor:
    """One row the reservation returned. This is the cohort, one donor at a time.

    Carries the three columns RETURNING gives us, because dispatch needs all
    three: the id to build the alert, the language to pick a template, and the
    ciphertext to decrypt at submission time. Nothing here is ever logged.
    """

    donor_id: UUID
    preferred_language: str
    msisdn_ciphertext: bytes


@dataclass
class MatchResult:
    radius_metres: int
    reserved_donors: list[ReservedDonor] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def donor_ids(self) -> list[UUID]:
        return [d.donor_id for d in self.reserved_donors]


def _local_date(now: datetime) -> date:
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(_HARARE).date()


def _diagnostics(
    session: Session,
    collection_point_id: UUID,
    abo: str,
    rh: str,
    radius: int,
    today: date,
    cap_cutoff: datetime,
) -> dict:
    rows = session.execute(
        _DIAGNOSTICS,
        {
            "cp_id": collection_point_id,
            "radius": radius,
            "abo": abo,
            "rh": rh,
            "today": today,
            "cap_cutoff": cap_cutoff,
        },
    ).all()

    buckets = {row.bucket: row.n for row in rows}
    self_declared = buckets.pop(SELF_DECLARED, 0)
    eligible = buckets.pop(ELIGIBLE, 0)
    excluded = {reason: count for reason, count in buckets.items() if count}

    return {
        "verified_in_radius": eligible + sum(excluded.values()),
        "self_declared_in_radius": self_declared,
        "eligible": eligible,
        "excluded": excluded,
    }


def match_and_reserve(
    session: Session,
    collection_point_id: UUID,
    abo: str,
    rh: str,
    units_required: int,
    now: datetime,
) -> MatchResult:
    """Reserve contact with a cohort of donors and return it.

    Runs in the caller's transaction. On return the reserved donors already
    have last_alerted_at set, so nothing else can claim them - which is why the
    caller must go on to write alert and outbox rows in this same transaction
    (ADR-010). A rollback releases the reservation cleanly; a commit without
    alert rows would burn the donors silently.
    """
    dispatch_cap = units_required * settings.dispatch_cap_multiplier
    cap_cutoff = now - timedelta(days=settings.frequency_cap_days)
    today = _local_date(now)

    reserved: list[ReservedDonor] = []
    bands_tried: list[int] = []
    radius = settings.radius_bands[0]
    diagnostics: dict = {}

    for band in settings.radius_bands:
        remaining = dispatch_cap - len(reserved)
        if remaining <= 0:
            break

        radius = band
        bands_tried.append(band)

        diagnostics = _diagnostics(
            session, collection_point_id, abo, rh, band, today, cap_cutoff
        )

        rows = session.execute(
            _RESERVE,
            {
                "now": now,
                "cp_id": collection_point_id,
                "radius": band,
                "abo": abo,
                "rh": rh,
                "today": today,
                "cap_cutoff": cap_cutoff,
                # The remaining budget, not the full cap. Donors reserved in a
                # narrower band are already held, and the ceiling is on the
                # request as a whole (FR-GEO-07).
                "dispatch_cap": remaining,
            },
        ).all()

        # Accumulate. A donor reserved at 3 km is not returned again at 5 km -
        # their last_alerted_at is now set, so the cap predicate excludes them.
        # Taking only the widest band's rows would leave those donors marked as
        # contacted but never actually messaged.
        reserved.extend(
            ReservedDonor(
                donor_id=row.id,
                preferred_language=row.preferred_language,
                msisdn_ciphertext=row.msisdn_ciphertext,
            )
            for row in rows
        )

        if len(reserved) >= units_required:
            break

    stats = {
        **diagnostics,
        "reserved": len(reserved),
        "escalated": len(bands_tried) > 1,
        "bands_tried": bands_tried,
    }

    return MatchResult(radius_metres=radius, reserved_donors=reserved, stats=stats)
