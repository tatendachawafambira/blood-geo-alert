"""Aggregate read model for the clinician dashboard. ADR-005, FR-GEO-08.

This module is the enforcement point, and the enforcement is structural rather
than careful. Every function returns a dict or a list of dicts built from
counts, names and derived timings. None takes a Donor, none returns one, and
none has a code path that could.

The threat this answers is not an external attacker. It is a member of staff
with a valid account trying to find out where one named person lives.
Encryption does not address that, authentication does not address it, and
access control only partly does. The robust answer is a system that cannot
answer the question - which is why ADR-005 forbids individual visibility
outright rather than restricting it to a privileged role. A privileged role
would eventually be granted to someone under pressure.

Ward centroids DO appear here, in ward_aggregates, and that is not a
contradiction. A ward centroid is reference data shared by every donor in the
ward; it is not a donor's location. The inference risk - a ward with one
alerted donor is close to naming them - is what the small-cell suppression
below exists to remove.
"""

from __future__ import annotations

import inspect
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# FR-ANA-04. A report saying one of three registered O-negative donors in a
# sparse ward responded is close to naming an individual.
SMALL_CELL_THRESHOLD = 3
OTHER_BUCKET = "Other wards"

ALERT_STATES = (
    "QUEUED",
    "SENT",
    "DELIVERED",
    "OPENED",
    "CONFIRMED",
    "DECLINED",
    "SUPPRESSED",
    "DEAD",
)

# Key names that would mean an individual had leaked into an aggregate.
_FORBIDDEN_KEYS = (
    "donor_id",
    "donor_ids",
    "msisdn",
    "phone",
    "mobile",
    "blind_index",
    "ciphertext",
    "token",
)

_SUMMARY = text(
    """
    SELECT dr.id, dr.abo, dr.rh, dr.units_required, dr.urgency, dr.radius_metres,
           dr.state, dr.rejection_reason, dr.matched_stats, dr.created_at,
           f.name AS facility_name,
           cp.name AS collection_point_name,
           CASE WHEN cp.id IS NULL THEN NULL ELSE
                ST_Distance(cp.location,
                            CAST(ST_SetSRID(ST_MakePoint(f.longitude, f.latitude), 4326)
                                 AS geography)) / 1000.0
           END AS distance_km,
           EXTRACT(EPOCH FROM (now() - dr.created_at)) AS elapsed_seconds
      FROM core.deficit_request dr
      JOIN core.facility f ON f.id = dr.facility_id
      LEFT JOIN core.collection_point cp ON cp.id = dr.collection_point_id
     WHERE dr.id = :request_id
    """
)

_PROGRESS = text(
    """
    SELECT a.state, count(*) AS n
      FROM core.alert a
     WHERE a.deficit_request_id = :request_id
     GROUP BY a.state
    """
)

# Counts per ward. The donor table is joined but never projected: nothing
# leaves this query except a ward name, a ward centroid and a count.
_WARD_AGGREGATES = text(
    """
    SELECT w.name AS ward_name,
           ST_Y(CAST(w.centroid AS geometry)) AS lat,
           ST_X(CAST(w.centroid AS geometry)) AS lon,
           count(*) AS alerted_count
      FROM core.alert a
      JOIN core.donor d ON d.id = a.donor_id
      JOIN core.ward w ON w.id = d.ward_id
     WHERE a.deficit_request_id = :request_id
     GROUP BY w.name, w.centroid
     ORDER BY count(*) DESC
    """
)


def _guard(payload: dict) -> dict:
    """Refuse to hand back anything shaped like an individual.

    A backstop, not the control. The control is that no query above selects a
    donor column. This catches the case where someone adds one later.
    """
    for key in payload:
        lowered = str(key).lower()
        if any(forbidden in lowered for forbidden in _FORBIDDEN_KEYS):
            raise AssertionError(
                f"readmodel tried to return {key!r}; ADR-005 permits counts and "
                f"aggregates only"
            )
    return payload


def request_summary(session: Session, request_id: UUID) -> dict:
    """Everything the detail page needs about the request itself."""
    row = session.execute(_SUMMARY, {"request_id": request_id}).first()
    if row is None:
        return {}

    return _guard(
        {
            "request_id": str(row.id),
            "facility_name": row.facility_name,
            "collection_point_name": row.collection_point_name,
            "distance_km": round(row.distance_km, 1) if row.distance_km is not None else None,
            "abo": row.abo,
            "rh": row.rh,
            "blood_group": f"{row.abo}{'+' if row.rh == 'POS' else '-'}",
            "units_required": row.units_required,
            "urgency": row.urgency,
            "radius_metres": row.radius_metres,
            "state": row.state,
            "rejection_reason": row.rejection_reason,
            "matched_stats": row.matched_stats or {},
            "created_at": row.created_at,
            "elapsed_seconds": int(row.elapsed_seconds or 0),
        }
    )


def alert_progress(session: Session, request_id: UUID) -> dict:
    """Counts by alert state, plus the units this request is trying to fill.

    Every state is present even at zero. A dashboard that hides empty states
    makes a request with no responses look like a request that is still
    loading - FR-DSH-04 requires an explicit zero, not an indeterminate one.
    """
    counts = {state: 0 for state in ALERT_STATES}
    for row in session.execute(_PROGRESS, {"request_id": request_id}):
        counts[row.state] = row.n

    units = session.execute(
        text("SELECT units_required FROM core.deficit_request WHERE id = :request_id"),
        {"request_id": request_id},
    ).scalar()

    return _guard(
        {
            "counts": counts,
            "total": sum(counts.values()),
            "units_required": units or 0,
            "confirmed": counts["CONFIRMED"],
            # ADR-018: at the moment of response only the reserved count is
            # known. Delivery is not.
            "pledged_label": f"{counts['CONFIRMED']} of {units or 0} units pledged",
        }
    )


def ward_aggregates(session: Session, request_id: UUID) -> list[dict]:
    """Alerted donors per ward, with small cells folded into one bucket.

    Suppression happens here rather than in the template, because a template
    is presentation and this is a privacy control (FR-ANA-04).
    """
    visible: list[dict] = []
    suppressed = 0

    for row in session.execute(_WARD_AGGREGATES, {"request_id": request_id}):
        if row.alerted_count < SMALL_CELL_THRESHOLD:
            suppressed += row.alerted_count
            continue
        visible.append(
            _guard(
                {
                    "ward_name": row.ward_name,
                    "lat": row.lat,
                    "lon": row.lon,
                    "alerted_count": row.alerted_count,
                }
            )
        )

    if suppressed:
        # No coordinate: the bucket spans several wards and pinning it
        # anywhere would be both wrong and a re-identification hint.
        visible.append(
            _guard(
                {
                    "ward_name": OTHER_BUCKET,
                    "lat": None,
                    "lon": None,
                    "alerted_count": suppressed,
                }
            )
        )

    return visible


def _validate_surface() -> None:
    """Fail at import if any public function could return an individual.

    Checked here rather than left to a reviewer: this is the module ADR-005
    rests on, and the failure mode of getting it wrong is silent.
    """
    allowed = {"dict", "list[dict]"}
    for name, function in list(globals().items()):
        if name.startswith("_") or not inspect.isfunction(function):
            continue
        if function.__module__ != __name__:
            continue
        annotation = inspect.signature(function).return_annotation
        rendered = getattr(annotation, "__name__", str(annotation))
        if rendered not in allowed:
            raise AssertionError(
                f"readmodel.{name} is annotated -> {rendered}; ADR-005 permits "
                f"only {sorted(allowed)}"
            )


_validate_surface()
