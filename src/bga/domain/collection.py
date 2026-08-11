"""Collection point opening hours and nearest-open-point resolution.

FR-COL-04 is enforced here, upstream of matching, so that a closed catchment
never reaches dispatch. Returning nothing is the correct behaviour when nothing
is open: alerting anyway sends willing people to a locked door, and they do not
come back a second time.

Times are evaluated in Africa/Harare. A datetime arriving as UTC would report
the wrong hour and the wrong weekday either side of midnight - 21:00 Harare is
19:00 UTC, and a Sunday-evening request read as Sunday 19:00 UTC is still
Sunday, but a 01:00 Monday request read in UTC is Sunday 23:00. Every hour and
weekday test below runs on the local value.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from bga.models.reference import CollectionPoint, Facility

HARARE = ZoneInfo("Africa/Harare")

SATURDAY = 5
SUNDAY = 6

# How far ahead next_opening() will look. Two weeks covers a Saturday-only
# mobile drive and any weekday pattern; beyond that the point is effectively
# not opening and saying so is more useful than a distant date.
_SEARCH_DAYS = 14

# ::geography is escaped as \:\: because text() would otherwise read it as a
# bind parameter. The SQL reaching Postgres is unchanged.
_NEAREST_ACTIVE = text(
    r"""
    SELECT id, name, ST_Distance(location,
             ST_SetSRID(ST_MakePoint(:lon,:lat),4326)\:\:geography) AS m
      FROM core.collection_point
     WHERE is_active
     ORDER BY m
    """
)


def _local(at: datetime) -> datetime:
    """Interpret `at` in Africa/Harare, whether it arrives naive or aware."""
    if at.tzinfo is None:
        return at.replace(tzinfo=HARARE)
    return at.astimezone(HARARE)


def is_open(point: CollectionPoint, at: datetime) -> bool:
    """True if this point can receive a donor at `at`."""
    if not point.is_active:
        return False

    local = _local(at)

    # Mobile drives are time-bounded; a drive that is not running must never
    # receive a donor (FR-COL-02).
    if point.active_from is not None and local < _local(point.active_from):
        return False
    if point.active_to is not None and local > _local(point.active_to):
        return False

    weekday = local.weekday()
    if weekday == SATURDAY and not point.open_saturday:
        return False
    if weekday == SUNDAY and not point.open_sunday:
        return False

    return point.open_hour <= local.hour < point.close_hour


def next_opening(point: CollectionPoint, at: datetime) -> datetime | None:
    """The earliest moment from `at` onwards at which this point is open.

    Returns `at` itself if the point is already open. Returns None if it does
    not open within the search horizon - a deactivated point, or one whose
    active window has closed.
    """
    if is_open(point, at):
        return _local(at)

    local = _local(at)
    for offset in range(_SEARCH_DAYS + 1):
        candidate = (local + timedelta(days=offset)).replace(
            hour=point.open_hour, minute=0, second=0, microsecond=0
        )
        if candidate >= local and is_open(point, candidate):
            return candidate

    return None


def _describe(moment: datetime) -> str:
    """'08:00 on Monday 13 August' - no %-d, which does not exist on Windows."""
    local = _local(moment)
    return f"{local:%H:%M} on {local:%A} {local.day} {local:%B}"


def resolve_collection_point(
    session: Session, facility: Facility, at: datetime
) -> tuple[CollectionPoint | None, str | None]:
    """Nearest OPEN collection point to the facility, or a reason there is none.

    The distance ordering is PostGIS work over a handful of rows. The open or
    closed decision is made in Python, because operating hours are a domain
    rule with a public holiday calendar and manual overrides ahead of it, not
    something to bury in SQL.
    """
    rows = session.execute(
        _NEAREST_ACTIVE, {"lat": facility.latitude, "lon": facility.longitude}
    ).all()

    if not rows:
        return None, (
            f"No collection point is open at {_describe(at)}."
            " No collection point is currently active."
        )

    for row in rows:
        point = session.get(CollectionPoint, row.id)
        if point is not None and is_open(point, at):
            return point, None

    # Nothing open. Name the nearest and say when it next opens, so the
    # facility can decide whether to wait or pursue alternative sourcing.
    nearest = session.get(CollectionPoint, rows[0].id)
    opens = next_opening(nearest, at) if nearest is not None else None

    if opens is None:
        return None, (
            f"No collection point is open at {_describe(at)}."
            f" The nearest, {rows[0].name}, has no scheduled opening."
        )

    return None, (
        f"No collection point is open at {_describe(at)}."
        f" The nearest, {rows[0].name}, opens at {_describe(opens)}."
    )
