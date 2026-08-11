"""Message composition, the GSM-7 budget, and quiet hours.

The tests that exercise create_alerts_and_outbox are marked to skip until
tokens.py exists (Block 6). They are deliberately NOT written against a stubbed
token issuer: a stub would keep passing after the real module lands and the
integration between them would never be tested. They activate on their own the
moment bga.domain.tokens is importable.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import re
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import cast, func, text

import bga.domain.dispatch as dispatch_module
from bga.config import settings
from bga.domain.dispatch import (
    MESSAGE_TEMPLATES,
    SEGMENT_SEPTETS,
    create_alerts_and_outbox,
    quiet_hours_active,
    septet_length,
    validate_templates,
)
from bga.models.donor import Donor, DonorEligibility
from bga.models.reference import CollectionPoint, Facility, Geography, Ward
from bga.models.requests import DeficitRequest

HARARE = ZoneInfo("Africa/Harare")
LANGUAGES = ("en", "sn", "nd")

requires_tokens = pytest.mark.skipif(
    importlib.util.find_spec("bga.domain.tokens") is None,
    reason="tokens.py arrives in Block 6",
)


def at(hour: int, day: int = 12) -> datetime:
    return datetime(2026, 8, day, hour, 0, tzinfo=HARARE)


# --- templates -------------------------------------------------------------


def test_templates_are_valid():
    """The build-time check. A template edit that would double the cost of
    every mobilisation fails here rather than on the day."""
    validate_templates()


def test_every_language_has_a_template():
    assert set(MESSAGE_TEMPLATES) == set(LANGUAGES)


@pytest.mark.parametrize("language", LANGUAGES)
def test_templates_expose_only_url_and_point_name(language):
    """FR-MSG-03 structurally: there is no placeholder for a blood group, a
    name or any clinical detail, so a template edit cannot introduce one."""
    assert sorted(re.findall(r"{(\w+)}", MESSAGE_TEMPLATES[language])) == [
        "point_name",
        "url",
    ]


@pytest.mark.parametrize("language", LANGUAGES)
def test_templates_name_the_sender(language):
    """An unattributed link asking someone to travel is indistinguishable
    from a phishing message."""
    assert "NBSZ" in MESSAGE_TEMPLATES[language]


def test_validate_templates_rejects_a_message_over_one_segment(monkeypatch):
    monkeypatch.setitem(
        MESSAGE_TEMPLATES, "en", "NBSZ: " + "x" * 140 + " {point_name}: {url}"
    )
    with pytest.raises(ValueError, match="over the 160 limit"):
        validate_templates()


def test_validate_templates_rejects_characters_that_force_ucs2(monkeypatch):
    """A single curly apostrophe halves the segment to 70 characters and
    splits every message we send."""
    monkeypatch.setitem(
        MESSAGE_TEMPLATES, "en", "NBSZ: blood’s needed. {point_name}: {url}"
    )
    with pytest.raises(ValueError, match="outside GSM-7"):
        validate_templates()


def test_septet_length_counts_escaped_characters_twice():
    assert septet_length("abc") == 3
    assert septet_length("a{b") == 4  # { costs two septets in GSM-7


def test_the_segment_budget_has_almost_no_slack():
    """Documents the real constraint rather than assuming it.

    A 43-character token plus a 44-character point name plus the base URL
    consumes most of 160 septets before a word is written. If this starts
    failing, the base URL or a collection point name has grown.
    """
    worst_url = f"{settings.donor_base_url}/d/{'T' * 43}"
    longest_name = "NBSZ Mobile Drive - Glen View Community Hall"

    for language, template in MESSAGE_TEMPLATES.items():
        length = septet_length(template.format(url=worst_url, point_name=longest_name))
        assert length <= SEGMENT_SEPTETS, f"{language} renders to {length} septets"


# --- quiet hours -----------------------------------------------------------


@pytest.mark.parametrize("hour", [21, 22, 23, 0, 3, 6])
def test_inside_quiet_hours(hour):
    assert quiet_hours_active(at(hour)) is True


@pytest.mark.parametrize("hour", [7, 8, 12, 17, 20])
def test_outside_quiet_hours(hour):
    assert quiet_hours_active(at(hour)) is False


def test_quiet_hours_boundaries_are_start_inclusive_end_exclusive():
    assert quiet_hours_active(at(20)) is False
    assert quiet_hours_active(at(21)) is True
    assert quiet_hours_active(at(6)) is True
    assert quiet_hours_active(at(7)) is False


def test_quiet_hours_are_evaluated_in_harare_not_utc():
    """21:00 Harare is 19:00 UTC. Read as UTC the window would open two hours
    early and defer routine requests that should have gone out."""
    from datetime import timezone

    nine_pm_as_utc = at(21).astimezone(timezone.utc)
    assert nine_pm_as_utc.hour == 19
    assert quiet_hours_active(nine_pm_as_utc) is True


def test_a_naive_datetime_is_read_as_harare_local():
    assert quiet_hours_active(datetime(2026, 8, 12, 22, 0)) is True
    assert quiet_hours_active(datetime(2026, 8, 12, 12, 0)) is False


# --- the outbox write (activates once tokens.py exists) --------------------


def point_at(lat: float, lon: float):
    return cast(func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326), Geography())


@pytest.fixture()
def catchment(db_session):
    facility = Facility(
        name=f"Parirenyatwa {uuid4().hex[:6]}", latitude=-17.8175, longitude=31.0405
    )
    point = CollectionPoint(
        name="NBSZ Mobile Drive - Glen View Community Hall",
        kind="MOBILE",
        location=point_at(-17.9070, 30.9750),
        open_hour=9,
        close_hour=15,
        is_active=True,
    )
    ward = Ward(name=f"ward-{uuid4().hex[:6]}", centroid=point_at(-17.83, 31.05))
    db_session.add_all([facility, point, ward])
    db_session.flush()

    donors = []
    for language in LANGUAGES:
        donor = Donor(
            msisdn_ciphertext=os.urandom(41),
            msisdn_blind_index=os.urandom(32),
            abo="O",
            rh="POS",
            sex="M",
            ward_id=ward.id,
            verification_state="NBSZ_VERIFIED",
            availability="AVAILABLE",
            preferred_language=language,
            age_band="EIGHTEEN_PLUS",
        )
        db_session.add(donor)
        db_session.flush()
        db_session.add(
            DonorEligibility(
                donor_id=donor.id, next_eligible_date="2020-01-01", deferral_flag=False
            )
        )
        donors.append(donor)
    db_session.flush()
    return facility, point, donors


def make_request(db_session, facility, point, urgency="URGENT"):
    request = DeficitRequest(
        facility_id=facility.id,
        collection_point_id=point.id,
        abo="O",
        rh="POS",
        units_required=2,
        urgency=urgency,
        radius_metres=3000,
        state="ACTIVE",
    )
    db_session.add(request)
    db_session.flush()
    return request


def reserved_from(donors):
    from bga.domain.matching import ReservedDonor

    return [
        ReservedDonor(
            donor_id=d.id,
            preferred_language=d.preferred_language,
            msisdn_ciphertext=d.msisdn_ciphertext,
        )
        for d in donors
    ]


@requires_tokens
def test_one_alert_and_one_outbox_row_per_donor(db_session, catchment, audit_sink):
    facility, point, donors = catchment
    request = make_request(db_session, facility, point)

    alerts = create_alerts_and_outbox(db_session, request, reserved_from(donors), at(10))

    assert len(alerts) == len(donors)
    assert {a.state for a in alerts} == {"QUEUED"}
    assert {a.state_rank for a in alerts} == {0}
    assert db_session.execute(text("SELECT count(*) FROM core.outbox")).scalar() == 3


@requires_tokens
def test_outbox_payload_shape(db_session, catchment, audit_sink):
    facility, point, donors = catchment
    request = make_request(db_session, facility, point)

    alerts = create_alerts_and_outbox(db_session, request, reserved_from(donors), at(10))
    payload = db_session.execute(text("SELECT payload FROM core.outbox LIMIT 1")).scalar()

    assert set(payload) == {
        "alert_id",
        "msisdn_ciphertext_b64",
        "body",
        "idempotency_key",
        "facility_id",
    }
    assert payload["idempotency_key"] == payload["alert_id"]
    assert base64.b64decode(payload["msisdn_ciphertext_b64"])
    assert payload["body"] in {a.message_body for a in alerts}


@requires_tokens
def test_payload_carries_the_facility_so_the_worker_can_scope_itself(
    db_session, catchment, audit_sink
):
    """core.alert has FORCE ROW LEVEL SECURITY. A worker with no facility
    scope sees zero alerts and updates zero rows, silently."""
    facility, point, donors = catchment
    request = make_request(db_session, facility, point)

    create_alerts_and_outbox(db_session, request, reserved_from(donors), at(10))
    payload = db_session.execute(text("SELECT payload FROM core.outbox LIMIT 1")).scalar()

    assert payload["facility_id"] == str(facility.id)


@requires_tokens
def test_every_rendered_message_fits_one_segment(db_session, catchment, audit_sink):
    facility, point, donors = catchment
    request = make_request(db_session, facility, point)

    alerts = create_alerts_and_outbox(db_session, request, reserved_from(donors), at(10))

    for alert in alerts:
        assert septet_length(alert.message_body) <= SEGMENT_SEPTETS


@requires_tokens
def test_each_donor_gets_their_own_language(db_session, catchment, audit_sink):
    facility, point, donors = catchment
    request = make_request(db_session, facility, point)

    alerts = create_alerts_and_outbox(db_session, request, reserved_from(donors), at(10))
    bodies = {a.donor_id: a.message_body for a in alerts}

    for donor in donors:
        expected_prefix = MESSAGE_TEMPLATES[donor.preferred_language].split("{")[0]
        assert bodies[donor.id].startswith(expected_prefix)


@requires_tokens
def test_no_gateway_is_called(db_session, catchment, audit_sink):
    """ADR-018: dispatch is asynchronous. Composition must not submit."""
    import inspect

    source = inspect.getsource(dispatch_module)
    assert "adapters" not in source
    assert ".submit(" not in source


@requires_tokens
def test_one_audit_event_for_the_batch_not_one_per_donor(
    db_session, catchment, audit_sink
):
    facility, point, donors = catchment
    request = make_request(db_session, facility, point)

    create_alerts_and_outbox(db_session, request, reserved_from(donors), at(10))

    rows = audit_sink()
    assert [r.action for r in rows] == ["ALERTS_QUEUED"]
    assert rows[0].detail["alerts"] == 3


@requires_tokens
def test_a_routine_request_in_quiet_hours_is_deferred(db_session, catchment, audit_sink):
    facility, point, donors = catchment
    request = make_request(db_session, facility, point, urgency="ROUTINE")

    create_alerts_and_outbox(db_session, request, reserved_from(donors), at(22))
    next_attempt = db_session.execute(
        text("SELECT next_attempt_at FROM core.outbox LIMIT 1")
    ).scalar()

    assert next_attempt.astimezone(HARARE).hour == settings.quiet_hours_end
    assert next_attempt.astimezone(HARARE).day == 13, "rolls to the next morning"


@pytest.mark.parametrize("urgency", ["URGENT", "CRITICAL"])
@requires_tokens
def test_urgent_and_critical_requests_are_exempt_from_quiet_hours(
    db_session, catchment, audit_sink, urgency
):
    facility, point, donors = catchment
    request = make_request(db_session, facility, point, urgency=urgency)

    create_alerts_and_outbox(db_session, request, reserved_from(donors), at(22))
    next_attempt = db_session.execute(
        text("SELECT next_attempt_at FROM core.outbox LIMIT 1")
    ).scalar()

    assert next_attempt.astimezone(HARARE).hour == 22, "dispatched immediately"
