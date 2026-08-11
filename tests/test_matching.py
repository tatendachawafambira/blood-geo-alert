"""Matching and atomic contact reservation. ADR-015.

Real database, real concurrency, no mocks. A mock cannot exhibit SKIP LOCKED,
and SKIP LOCKED is the only thing being tested that actually matters.

Distances are inserted straight into core.ward_collection_proximity with
explicit metres, so a band boundary is exact rather than approximately right.
That table is precomputed in production too; matching never sees geography.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import cast, func, text

from bga.config import settings
from bga.domain.matching import match_and_reserve
from bga.models.donor import Donor, DonorEligibility
from bga.models.reference import CollectionPoint, Geography, Ward

from zoneinfo import ZoneInfo

HARARE = ZoneInfo("Africa/Harare")
NOW = datetime(2026, 8, 11, 10, 0, tzinfo=HARARE)
LONG_AGO = date(2020, 1, 1)

NEAR, MID, FAR = 1000, 4000, 8000  # metres: inside band 1, 2 and 3 respectively


def point_at(lat: float, lon: float):
    return cast(func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326), Geography())


def build_catchment(session):
    """One collection point and three wards at 1 km, 4 km and 8 km."""
    cp = CollectionPoint(
        name=f"NBSZ probe {uuid4().hex[:8]}",
        kind="BRANCH",
        location=point_at(-17.8180, 31.0400),
        open_hour=8,
        close_hour=16,
        is_active=True,
    )
    session.add(cp)
    session.flush()

    wards = {}
    for label, metres in (("near", NEAR), ("mid", MID), ("far", FAR)):
        ward = Ward(name=f"ward-{label}-{uuid4().hex[:8]}", centroid=point_at(-17.83, 31.05))
        session.add(ward)
        session.flush()
        session.execute(
            text(
                "INSERT INTO core.ward_collection_proximity"
                " (ward_id, collection_point_id, metres) VALUES (:w, :c, :m)"
            ),
            {"w": ward.id, "c": cp.id, "m": float(metres)},
        )
        wards[label] = ward

    return cp, wards


def add_donor(
    session,
    ward,
    *,
    abo="O",
    rh="POS",
    verification_state="NBSZ_VERIFIED",
    availability="AVAILABLE",
    age_band="EIGHTEEN_PLUS",
    opt_out_at=None,
    last_alerted_at=None,
    deferral_flag=False,
    next_eligible_date=LONG_AGO,
    donations_last_12m=0,
    preferred_language="en",
):
    """An eligible verified adult unless a keyword says otherwise."""
    donor = Donor(
        msisdn_ciphertext=os.urandom(28),
        msisdn_blind_index=os.urandom(32),
        abo=abo,
        rh=rh,
        sex="M",
        ward_id=ward.id,
        verification_state=verification_state,
        availability=availability,
        preferred_language=preferred_language,
        age_band=age_band,
        opt_out_at=opt_out_at,
        last_alerted_at=last_alerted_at,
    )
    session.add(donor)
    session.flush()
    session.add(
        DonorEligibility(
            donor_id=donor.id,
            next_eligible_date=next_eligible_date,
            deferral_flag=deferral_flag,
            donations_last_12m=donations_last_12m,
        )
    )
    session.flush()
    return donor


@pytest.fixture()
def catchment(db_session):
    return build_catchment(db_session)


# --- the frequency cap -----------------------------------------------------


def test_donor_alerted_three_days_ago_is_not_reserved(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"], last_alerted_at=NOW - timedelta(days=3))

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []
    assert result.stats["excluded"].get("CONTACTED_RECENTLY") == 1


def test_donor_alerted_twenty_days_ago_is_reserved(db_session, catchment):
    cp, wards = catchment
    donor = add_donor(db_session, wards["near"], last_alerted_at=NOW - timedelta(days=20))

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == [donor.id]


def test_cap_boundary_is_the_configured_window(db_session, catchment):
    cp, wards = catchment
    just_inside = add_donor(
        db_session,
        wards["near"],
        last_alerted_at=NOW - timedelta(days=settings.frequency_cap_days, hours=1),
    )
    add_donor(
        db_session,
        wards["near"],
        last_alerted_at=NOW - timedelta(days=settings.frequency_cap_days, hours=-1),
    )

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == [just_inside.id]


# --- exclusions the reservation enforces -----------------------------------


def test_self_declared_donor_is_never_reserved(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"], verification_state="SELF_DECLARED")

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []
    assert result.stats["self_declared_in_radius"] == 1
    assert result.stats["verified_in_radius"] == 0


def test_deferred_donor_is_never_reserved(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"], deferral_flag=True)

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []
    assert result.stats["excluded"].get("DEFERRED") == 1


def test_opted_out_donor_is_never_reserved(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"], opt_out_at=NOW - timedelta(days=1))

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []
    assert result.stats["excluded"].get("OPTED_OUT") == 1


def test_paused_donor_is_never_reserved(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"], availability="PAUSED")

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []
    assert result.stats["excluded"].get("PAUSED") == 1


def test_under_18_donor_is_never_reserved(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"], age_band="UNDER_18")

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []
    assert result.stats["excluded"].get("UNDER_18") == 1


def test_donor_inside_their_donation_interval_is_never_reserved(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"], next_eligible_date=date(2027, 1, 1))

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []
    assert result.stats["excluded"].get("INTERVAL_NOT_ELAPSED") == 1


def test_a_different_blood_group_is_not_matched(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"], abo="A", rh="NEG")

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []
    assert result.stats["verified_in_radius"] == 0


def test_a_donor_outside_the_widest_band_is_never_matched(db_session, catchment):
    cp, wards = catchment
    outside = Ward(name=f"ward-outside-{uuid4().hex[:8]}", centroid=point_at(-18.01, 31.07))
    db_session.add(outside)
    db_session.flush()
    db_session.execute(
        text(
            "INSERT INTO core.ward_collection_proximity"
            " (ward_id, collection_point_id, metres) VALUES (:w, :c, 21888)"
        ),
        {"w": outside.id, "c": cp.id},
    )
    add_donor(db_session, outside)

    result = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert result.donor_ids == []


# --- the dispatch cap ------------------------------------------------------


def test_cohort_is_capped_at_units_required_times_multiplier(db_session, catchment):
    cp, wards = catchment
    for _ in range(15):
        add_donor(db_session, wards["near"])

    result = match_and_reserve(db_session, cp.id, "O", "POS", 1, NOW)

    assert len(result.donor_ids) == 1 * settings.dispatch_cap_multiplier == 10
    assert result.stats["reserved"] == 10
    assert result.stats["eligible"] == 15, "all 15 were eligible; only 10 were taken"


# --- escalation ------------------------------------------------------------


def test_no_escalation_when_the_first_band_is_sufficient(db_session, catchment):
    cp, wards = catchment
    for _ in range(3):
        add_donor(db_session, wards["near"])

    result = match_and_reserve(db_session, cp.id, "O", "POS", 3, NOW)

    assert result.stats["escalated"] is False
    assert result.stats["bands_tried"] == [settings.radius_bands[0]]
    assert result.radius_metres == settings.radius_bands[0]


def test_escalates_to_a_wider_band_when_the_first_is_too_thin(db_session, catchment):
    cp, wards = catchment
    near_donor = add_donor(db_session, wards["near"])  # 1 km, inside band 1
    mid_a = add_donor(db_session, wards["mid"])  # 4 km, needs band 2
    mid_b = add_donor(db_session, wards["mid"])

    result = match_and_reserve(db_session, cp.id, "O", "POS", 3, NOW)

    assert result.stats["escalated"] is True
    assert result.stats["bands_tried"] == [3000, 5000]
    assert result.radius_metres == 5000
    # The band-1 donor is carried forward, not dropped: the wider band cannot
    # return them again because their last_alerted_at is now set.
    assert set(result.donor_ids) == {near_donor.id, mid_a.id, mid_b.id}
    assert result.stats["reserved"] == 3


def test_escalation_stops_at_the_widest_band(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"])
    add_donor(db_session, wards["far"])  # 8 km, needs band 3

    result = match_and_reserve(db_session, cp.id, "O", "POS", 50, NOW)

    assert result.stats["bands_tried"] == list(settings.radius_bands)
    assert result.radius_metres == settings.radius_bands[-1]
    assert result.stats["reserved"] == 2, "never invents donors to meet the target"


# --- the reservation is a write --------------------------------------------


def test_reserved_donors_have_last_alerted_at_stamped(db_session, catchment):
    cp, wards = catchment
    donor = add_donor(db_session, wards["near"])

    match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    stamped = db_session.execute(
        text("SELECT last_alerted_at FROM core.donor WHERE id = :id"), {"id": donor.id}
    ).scalar_one()
    assert stamped == NOW


def test_a_second_request_cannot_take_the_same_donors(db_session, catchment):
    """Sequential proof of the cap: the same catchment, twice, one transaction."""
    cp, wards = catchment
    for _ in range(4):
        add_donor(db_session, wards["near"])

    first = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)
    second = match_and_reserve(db_session, cp.id, "O", "POS", 4, NOW)

    assert len(first.donor_ids) == 4
    assert second.donor_ids == []


# --- stats -----------------------------------------------------------------


def test_stats_buckets_account_for_every_verified_donor_in_radius(db_session, catchment):
    cp, wards = catchment
    add_donor(db_session, wards["near"])
    add_donor(db_session, wards["near"], deferral_flag=True)
    add_donor(db_session, wards["near"], availability="PAUSED")
    add_donor(db_session, wards["near"], opt_out_at=NOW)
    add_donor(db_session, wards["near"], verification_state="SELF_DECLARED")

    result = match_and_reserve(db_session, cp.id, "O", "POS", 1, NOW)
    stats = result.stats

    assert stats["self_declared_in_radius"] == 1
    assert stats["verified_in_radius"] == 4
    assert stats["eligible"] == 1
    assert stats["verified_in_radius"] == stats["eligible"] + sum(stats["excluded"].values())


def test_stats_are_json_serialisable_for_the_jsonb_column(db_session, catchment):
    import json

    cp, wards = catchment
    add_donor(db_session, wards["near"])

    result = match_and_reserve(db_session, cp.id, "O", "POS", 1, NOW)
    assert json.loads(json.dumps(result.stats))["reserved"] == 1


# --- concurrency: the one that matters -------------------------------------


@pytest.fixture()
def committed_catchment(session_factory):
    """Data the concurrency test needs, committed so two connections can see it.

    Every other test rolls back inside a single connection. This one cannot:
    two transactions have to be genuinely in flight at the same time, on
    separate connections, or SKIP LOCKED has nothing to skip.
    """
    setup = session_factory()
    cp, wards = build_catchment(setup)
    for _ in range(20):
        add_donor(setup, wards["near"])
    setup.commit()
    cp_id = cp.id
    setup.close()

    yield cp_id

    teardown = session_factory()
    teardown.execute(
        text(
            "TRUNCATE core.donor_eligibility, core.donor,"
            " core.ward_collection_proximity, core.ward, core.collection_point CASCADE"
        )
    )
    teardown.commit()
    teardown.close()


def test_concurrency_two_requests_reserve_disjoint_donor_sets(
    session_factory, committed_catchment
):
    """The most important test in the codebase.

    Two hospitals raise a request over the same catchment in the same moment.
    Neither transaction has committed while the other runs, so neither can see
    the other's last_alerted_at writes. The only thing keeping them apart is
    FOR UPDATE ... SKIP LOCKED.

    Both sessions are left open across the two calls deliberately. If the first
    committed before the second ran, the second would be excluded by the cap
    predicate instead and the test would pass without ever exercising the lock.

    20 eligible donors, units_required=1, so dispatch_cap is 10: the first
    request takes 10 and the second must take the other 10. If SKIP LOCKED were
    dropped, the second would block until the first finished; if the whole
    thing were rewritten as SELECT-then-UPDATE, both would return the same 10.
    """
    cp_id = committed_catchment
    hospital_a = session_factory()
    hospital_b = session_factory()

    try:
        result_a = match_and_reserve(hospital_a, cp_id, "O", "POS", 1, NOW)
        # A is still open; its rows are locked and its writes are invisible to B.
        result_b = match_and_reserve(hospital_b, cp_id, "O", "POS", 1, NOW)

        set_a = set(result_a.donor_ids)
        set_b = set(result_b.donor_ids)

        assert len(set_a) == 10, "first request should fill its dispatch cap"
        assert len(set_b) == 10, "second request should find the untaken donors"
        assert set_a.isdisjoint(set_b), "THE property: no donor is alerted twice"
        assert len(set_a | set_b) == 20

        # Each request's own view of what it reserved is what it returned.
        assert result_a.stats["reserved"] == 10
        assert result_b.stats["reserved"] == 10
    finally:
        hospital_a.rollback()
        hospital_b.rollback()
        hospital_a.close()
        hospital_b.close()


def test_concurrency_second_request_does_not_block_on_the_first(
    session_factory, committed_catchment
):
    """SKIP LOCKED, not a queue.

    If the second request waited for the first, this would sit on a lock until
    the statement timeout rather than returning. Timing it is crude, but a
    blocked request is off by seconds, not milliseconds.
    """
    import time

    cp_id = committed_catchment
    hospital_a = session_factory()
    hospital_b = session_factory()

    try:
        match_and_reserve(hospital_a, cp_id, "O", "POS", 1, NOW)

        started = time.monotonic()
        match_and_reserve(hospital_b, cp_id, "O", "POS", 1, NOW)
        elapsed = time.monotonic() - started

        assert elapsed < 2.0, f"second request appears to have blocked ({elapsed:.1f}s)"
    finally:
        hospital_a.rollback()
        hospital_b.rollback()
        hospital_a.close()
        hospital_b.close()
