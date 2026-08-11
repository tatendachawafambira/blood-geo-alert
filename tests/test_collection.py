"""Opening hours and nearest-open-point resolution.

is_open and next_opening are pure, so most of this needs no database. The
resolve_collection_point tests use the migrated test database, because the
distance ordering is genuinely PostGIS work.

12 August 2026 is a Wednesday. The Sunday used throughout is 16 August 2026 and
the Monday after it is 17 August 2026.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import cast, func

from bga.domain.collection import HARARE, is_open, next_opening, resolve_collection_point
from bga.models.reference import CollectionPoint, Facility, Geography


def at(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=HARARE)


WEDNESDAY_1000 = at(2026, 8, 12, 10)
SATURDAY_1000 = at(2026, 8, 15, 10)
SUNDAY_2100 = at(2026, 8, 16, 21)
SUNDAY_1000 = at(2026, 8, 16, 10)
MONDAY_0800 = at(2026, 8, 17, 8)


def branch(**overrides) -> CollectionPoint:
    """NBSZ Harare Branch: weekdays 08-16, open Saturday, closed Sunday."""
    defaults = dict(
        name="NBSZ Harare Branch",
        kind="BRANCH",
        open_hour=8,
        close_hour=16,
        open_saturday=True,
        open_sunday=False,
        active_from=None,
        active_to=None,
        is_active=True,
    )
    return CollectionPoint(**{**defaults, **overrides})


# --- is_open ---------------------------------------------------------------


def test_open_during_weekday_hours():
    assert is_open(branch(), WEDNESDAY_1000) is True


def test_closed_outside_hours_on_an_open_day():
    assert is_open(branch(), at(2026, 8, 12, 7, 59)) is False
    assert is_open(branch(), at(2026, 8, 12, 16)) is False, "close_hour is exclusive"
    assert is_open(branch(), at(2026, 8, 12, 15, 59)) is True


def test_open_on_saturday_when_open_saturday_is_set():
    assert is_open(branch(), SATURDAY_1000) is True


def test_closed_on_saturday_when_open_saturday_is_not_set():
    assert is_open(branch(open_saturday=False), SATURDAY_1000) is False


def test_closed_on_sunday_for_a_sunday_closed_point():
    # Even at 10:00, inside the normal weekday window.
    assert is_open(branch(), SUNDAY_1000) is False


def test_closed_on_sunday_evening():
    # The scenario the demo turns on: 21:00 on a Sunday.
    assert is_open(branch(), SUNDAY_2100) is False


def test_open_on_sunday_when_open_sunday_is_set():
    assert is_open(branch(open_sunday=True), SUNDAY_1000) is True


def test_inactive_mobile_point_is_never_open():
    mobile = branch(
        name="NBSZ Mobile Drive - Highfield", kind="MOBILE", is_active=False, open_hour=9,
        close_hour=15,
    )
    assert is_open(mobile, WEDNESDAY_1000) is False


def test_mobile_point_outside_its_active_window_is_closed():
    mobile = branch(
        name="NBSZ Mobile Drive - Glen View",
        kind="MOBILE",
        open_hour=9,
        close_hour=15,
        active_from=at(2026, 9, 1, 0),
        active_to=at(2026, 9, 5, 0),
    )
    assert is_open(mobile, WEDNESDAY_1000) is False, "before the drive starts"
    assert is_open(mobile, at(2026, 9, 2, 10)) is True, "during the drive"
    assert is_open(mobile, at(2026, 9, 10, 10)) is False, "after the drive ends"


def test_a_utc_timestamp_is_evaluated_in_harare_time():
    """21:00 Harare is 19:00 UTC. Read as UTC the hour test would be wrong."""
    from datetime import timezone

    sunday_2100_as_utc = SUNDAY_2100.astimezone(timezone.utc)
    assert sunday_2100_as_utc.hour == 19
    assert is_open(branch(), sunday_2100_as_utc) is False

    wednesday_1000_as_utc = WEDNESDAY_1000.astimezone(timezone.utc)
    assert wednesday_1000_as_utc.hour == 8
    assert is_open(branch(), wednesday_1000_as_utc) is True


# --- next_opening ----------------------------------------------------------


def test_next_opening_rolls_sunday_night_to_monday_morning():
    assert next_opening(branch(), SUNDAY_2100) == MONDAY_0800


def test_next_opening_is_now_when_already_open():
    assert next_opening(branch(), WEDNESDAY_1000) == WEDNESDAY_1000


def test_next_opening_later_the_same_day_when_before_opening():
    assert next_opening(branch(), at(2026, 8, 12, 6)) == at(2026, 8, 12, 8)


def test_next_opening_skips_saturday_for_a_weekday_only_point():
    weekday_only = branch(open_saturday=False)
    # Friday evening, closed all weekend -> Monday.
    assert next_opening(weekday_only, at(2026, 8, 14, 18)) == MONDAY_0800


def test_next_opening_returns_none_for_an_inactive_point():
    assert next_opening(branch(is_active=False), WEDNESDAY_1000) is None


def test_next_opening_returns_none_after_a_mobile_drive_has_ended():
    ended = branch(kind="MOBILE", active_to=at(2026, 8, 1, 0))
    assert next_opening(ended, WEDNESDAY_1000) is None


# --- resolve_collection_point ---------------------------------------------


def point_at(lat: float, lon: float):
    """A geography(Point,4326) value, built by PostGIS as everywhere else."""
    return cast(func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326), Geography())


@pytest.fixture()
def harare(db_session):
    """Parirenyatwa, the NBSZ branch nearby, and a more distant mobile drive."""
    facility = Facility(
        name="Parirenyatwa Group of Hospitals", latitude=-17.8175, longitude=31.0405
    )

    near = branch()
    near.location = point_at(-17.8180, 31.0400)

    far = CollectionPoint(
        name="NBSZ Mobile Drive - Glen View",
        kind="MOBILE",
        location=point_at(-17.9070, 30.9750),
        open_hour=9,
        close_hour=15,
        open_saturday=True,
        open_sunday=False,
        is_active=True,
    )

    db_session.add_all([facility, near, far])
    db_session.flush()
    return facility, near, far


def test_resolves_to_the_nearest_open_point(db_session, harare):
    facility, near, _far = harare
    point, reason = resolve_collection_point(db_session, facility, WEDNESDAY_1000)
    assert reason is None
    assert point.id == near.id


def test_skips_the_nearest_when_it_is_closed_and_takes_the_next(db_session, harare):
    facility, near, far = harare
    # Close the branch; the mobile drive is still open at 10:00 on a Wednesday.
    near.is_active = False
    db_session.flush()

    point, reason = resolve_collection_point(db_session, facility, WEDNESDAY_1000)
    assert reason is None
    assert point.id == far.id


def test_sunday_evening_returns_no_point_and_an_explanatory_reason(db_session, harare):
    facility, _near, _far = harare
    point, reason = resolve_collection_point(db_session, facility, SUNDAY_2100)

    assert point is None, "FR-COL-04: nothing open means nothing is dispatched"
    assert reason == (
        "No collection point is open at 21:00 on Sunday 16 August."
        " The nearest, NBSZ Harare Branch, opens at 08:00 on Monday 17 August."
    )


def test_reason_is_a_full_sentence_naming_the_point_and_when_it_opens(db_session, harare):
    facility, _near, _far = harare
    _point, reason = resolve_collection_point(db_session, facility, SUNDAY_2100)
    assert reason.endswith(".")
    assert "NBSZ Harare Branch" in reason
    assert "Monday" in reason


def test_no_active_points_at_all_still_returns_a_reason(db_session, harare):
    facility, near, far = harare
    near.is_active = False
    far.is_active = False
    db_session.flush()

    point, reason = resolve_collection_point(db_session, facility, WEDNESDAY_1000)
    assert point is None
    assert "No collection point is currently active." in reason
