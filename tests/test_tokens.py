"""Response tokens and the donor application. FR-TOK, ADR-016.

The test that matters most is the indistinguishability one. Invalid, expired
and already-consumed tokens must produce byte-identical responses: any
difference is an oracle telling whoever holds the link whether the token was
ever real.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import cast, func, text
from sqlalchemy.orm import sessionmaker

from bga.api import donor as donor_routes
from bga.api.donor_main import app as donor_app
from bga.domain.tokens import issue_token, peek, redeem
from bga.models.donor import Donor, DonorEligibility
from bga.models.reference import CollectionPoint, Facility, Geography, Ward
from bga.models.requests import Alert, DeficitRequest, ResponseToken


def point_at(lat: float, lon: float):
    return cast(func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326), Geography())


@pytest.fixture()
def alert(db_session) -> Alert:
    facility = Facility(
        name=f"Harare Central {uuid4().hex[:6]}", latitude=-17.8630, longitude=31.0090
    )
    branch = CollectionPoint(
        name="NBSZ Harare Branch",
        kind="BRANCH",
        location=point_at(-17.8180, 31.0400),
        open_hour=8,
        close_hour=16,
        open_saturday=True,
        open_sunday=False,
        is_active=True,
    )
    ward = Ward(name=f"ward-{uuid4().hex[:6]}", centroid=point_at(-17.86, 31.03))
    db_session.add_all([facility, branch, ward])
    db_session.flush()

    person = Donor(
        msisdn_ciphertext=os.urandom(41),
        msisdn_blind_index=os.urandom(32),
        abo="O",
        rh="POS",
        sex="M",
        ward_id=ward.id,
        verification_state="NBSZ_VERIFIED",
        availability="AVAILABLE",
        preferred_language="en",
        age_band="EIGHTEEN_PLUS",
    )
    db_session.add(person)
    db_session.flush()
    db_session.add(DonorEligibility(donor_id=person.id, next_eligible_date="2020-01-01"))

    request = DeficitRequest(
        facility_id=facility.id,
        collection_point_id=branch.id,
        abo="O",
        rh="POS",
        units_required=4,
        urgency="URGENT",
        radius_metres=3000,
        state="ACTIVE",
    )
    db_session.add(request)
    db_session.flush()

    row = Alert(
        deficit_request_id=request.id,
        donor_id=person.id,
        channel="SMS",
        state="SENT",
        state_rank=1,
        provider_message_id=f"mock-{uuid4()}",
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture()
def client(db_session, monkeypatch) -> TestClient:
    """Drive the donor app against the test database.

    The routes are exercised through the owner connection, so this proves the
    behaviour, not the narrowness of the bga_donor role. That is a database
    grant and is verified in psql - see test_donor_role_cannot_reach_the_register
    below for the import-level half.
    """

    class BoundSession:
        def __call__(self):
            return _NonClosingSession(db_session)

    monkeypatch.setattr(donor_routes, "DonorSessionLocal", BoundSession())
    return TestClient(donor_app)


class _NonClosingSession:
    """Lends the test's session to a route without letting it commit or close.

    The route does `with Session() as s, s.begin():`. The test's transaction is
    already open and must survive for the assertions, so begin() opens a
    SAVEPOINT instead - a plain begin() on an active session raises
    "A transaction is already begun on this Session" - and close() is a no-op.
    """

    def __init__(self, session):
        self._session = session

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def begin(self):
        return self._session.begin_nested()

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._session, name)


# --- issue -----------------------------------------------------------------


def test_issue_returns_the_raw_token_and_stores_only_its_hash(db_session, alert):
    raw = issue_token(db_session, alert, 120)

    stored = db_session.execute(text("SELECT token_hash FROM core.response_token")).scalar()
    assert bytes(stored) == sha256(raw.encode()).digest()
    assert raw.encode() not in bytes(stored)


def test_the_raw_token_is_never_persisted_anywhere(db_session, alert):
    raw = issue_token(db_session, alert, 120)

    rows = db_session.execute(text("SELECT token_hash, alert_id FROM core.response_token")).all()
    assert all(raw not in str(value) for row in rows for value in row)


def test_tokens_carry_at_least_128_bits_of_entropy(db_session, alert):
    raw = issue_token(db_session, alert, 120)
    assert len(raw) >= 43  # secrets.token_urlsafe(32)


def test_two_tokens_are_never_the_same(db_session, alert):
    first = issue_token(db_session, alert, 120)
    db_session.execute(text("DELETE FROM core.response_token"))
    second = issue_token(db_session, alert, 120)
    assert first != second


# --- peek ------------------------------------------------------------------


def test_peek_resolves_a_valid_token_without_consuming_it(db_session, alert):
    raw = issue_token(db_session, alert, 120)

    assert peek(db_session, raw).id == alert.id
    assert peek(db_session, raw).id == alert.id, "peeking twice must still work"

    consumed = db_session.execute(text("SELECT consumed_at FROM core.response_token")).scalar()
    assert consumed is None


def test_peek_returns_none_for_a_fabricated_token(db_session, alert):
    issue_token(db_session, alert, 120)
    assert peek(db_session, "not-a-real-token") is None


def test_peek_returns_none_for_an_expired_token(db_session, alert):
    raw = issue_token(db_session, alert, 120)
    db_session.execute(
        text("UPDATE core.response_token SET expires_at = now() - interval '1 minute'")
    )
    assert peek(db_session, raw) is None


def test_peek_returns_none_for_an_empty_token(db_session):
    assert peek(db_session, "") is None


# --- redeem ----------------------------------------------------------------


def test_redeem_returns_the_alert_the_first_time(db_session, alert):
    raw = issue_token(db_session, alert, 120)
    assert redeem(db_session, raw).id == alert.id


def test_redeem_twice_returns_none_the_second_time(db_session, alert):
    """QAS-04: a donor taps twice, or forwards the link to a family member.
    Exactly one response is recorded."""
    raw = issue_token(db_session, alert, 120)

    assert redeem(db_session, raw) is not None
    assert redeem(db_session, raw) is None


def test_redeem_returns_none_for_an_expired_token(db_session, alert):
    raw = issue_token(db_session, alert, 120)
    db_session.execute(
        text("UPDATE core.response_token SET expires_at = now() - interval '1 minute'")
    )
    assert redeem(db_session, raw) is None


def test_redeem_returns_none_for_a_fabricated_token(db_session, alert):
    issue_token(db_session, alert, 120)
    assert redeem(db_session, "not-a-real-token") is None


def test_redeem_stamps_consumed_at(db_session, alert):
    raw = issue_token(db_session, alert, 120)
    redeem(db_session, raw)

    consumed = db_session.execute(text("SELECT consumed_at FROM core.response_token")).scalar()
    assert consumed is not None


def test_an_expired_token_is_not_consumed_by_a_failed_redeem(db_session, alert):
    """Nothing is mutated on the failure path, so a clock correction cannot
    turn a rejected attempt into a used one."""
    raw = issue_token(db_session, alert, 120)
    db_session.execute(
        text("UPDATE core.response_token SET expires_at = now() - interval '1 minute'")
    )
    redeem(db_session, raw)

    consumed = db_session.execute(text("SELECT consumed_at FROM core.response_token")).scalar()
    assert consumed is None


# --- indistinguishability (FR-TOK-11) --------------------------------------


def test_invalid_expired_and_consumed_produce_byte_identical_responses(
    db_session, alert, client, audit_sink
):
    """The one that matters. Any difference between these three is an oracle."""
    fabricated = client.get("/d/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    expired_raw = issue_token(db_session, alert, 120)
    db_session.execute(
        text("UPDATE core.response_token SET expires_at = now() - interval '1 minute'")
    )
    expired = client.get(f"/d/{expired_raw}")

    db_session.execute(text("DELETE FROM core.response_token"))
    consumed_raw = issue_token(db_session, alert, 120)
    client.post(f"/d/{consumed_raw}", data={"response": "confirm"})
    consumed = client.get(f"/d/{consumed_raw}")

    assert fabricated.status_code == expired.status_code == consumed.status_code == 200
    assert fabricated.content == expired.content == consumed.content
    assert fabricated.headers["content-type"] == consumed.headers["content-type"]


def test_the_closed_page_names_no_specific_cause(db_session, alert, client):
    """Generic copy is fine - "links can only be used once" appears on all
    three paths and so distinguishes nothing. What must never appear is
    wording that identifies WHICH case this was. The byte-identity test above
    is the real guarantee; this one guards the wording."""
    body = client.get("/d/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA").text.lower()

    for leak in (
        "expired",
        "already",
        "invalid",
        "not found",
        "unknown",
        "consumed",
        "does not exist",
    ):
        assert leak not in body, f"the closed page discloses the cause: {leak!r}"


def test_posting_to_a_dead_token_returns_the_same_closed_page(db_session, alert, client):
    on_get = client.get("/d/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA").content
    on_post = client.post(
        "/d/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", data={"response": "confirm"}
    ).content

    assert on_get == on_post


# --- the donor page --------------------------------------------------------


def test_the_page_shows_the_collection_point_and_two_buttons(db_session, alert, client):
    raw = issue_token(db_session, alert, 120)

    body = client.get(f"/d/{raw}").text

    assert "NBSZ Harare Branch" in body
    assert body.count("<button") == 2
    assert 'value="confirm"' in body and 'value="decline"' in body


def test_the_page_discloses_no_donor_or_clinical_detail(db_session, alert, client):
    """FR-TOK-07. Handsets are shared."""
    raw = issue_token(db_session, alert, 120)

    body = client.get(f"/d/{raw}").text.lower()

    for leak in ("o+", "o positive", "blood group", "donation history", "patient"):
        assert leak not in body


def test_the_page_needs_no_javascript(db_session, alert, client):
    """FR-TOK-12: the core function must work with JavaScript disabled."""
    raw = issue_token(db_session, alert, 120)

    body = client.get(f"/d/{raw}").text

    assert "<script" not in body.lower()
    assert 'method="post"' in body.lower()


def test_the_page_makes_no_third_party_requests(db_session, alert, client):
    """FR-TOK-13: no analytics, no advertising, no tracking."""
    raw = issue_token(db_session, alert, 120)

    body = client.get(f"/d/{raw}").text.lower()

    assert "http://" not in body.replace('href="/', "")
    for host in ("googletagmanager", "google-analytics", "facebook", "cdn.", "fonts."):
        assert host not in body


def test_the_whole_page_is_far_inside_the_fifty_kilobyte_budget(db_session, alert, client):
    raw = issue_token(db_session, alert, 120)

    html = client.get(f"/d/{raw}").content
    css = client.get("/static/donor.css").content

    assert len(css) < 3 * 1024, f"donor.css is {len(css)} bytes"
    assert len(html) + len(css) < 50 * 1024


# --- responding ------------------------------------------------------------


def test_confirming_advances_the_alert_and_offers_directions(
    db_session, alert, client, audit_sink
):
    raw = issue_token(db_session, alert, 120)

    body = client.post(f"/d/{raw}", data={"response": "confirm"}).text

    state, rank, responded = db_session.execute(
        text("SELECT state, state_rank, responded_at FROM core.alert WHERE id = :i"),
        {"i": alert.id},
    ).first()
    assert (state, rank) == ("CONFIRMED", 4)
    assert responded is not None
    assert "https://www.google.com/maps/dir/?api=1&amp;destination=" in body


def test_declining_advances_the_alert_without_directions(
    db_session, alert, client, audit_sink
):
    raw = issue_token(db_session, alert, 120)

    body = client.post(f"/d/{raw}", data={"response": "decline"}).text

    state, rank = db_session.execute(
        text("SELECT state, state_rank FROM core.alert WHERE id = :i"), {"i": alert.id}
    ).first()
    assert (state, rank) == ("DECLINED", 4)
    assert "maps/dir" not in body


def test_opening_the_link_records_it_as_opened(db_session, alert, client):
    raw = issue_token(db_session, alert, 120)

    client.get(f"/d/{raw}")

    state, rank = db_session.execute(
        text("SELECT state, state_rank FROM core.alert WHERE id = :i"), {"i": alert.id}
    ).first()
    assert (state, rank) == ("OPENED", 3)


def test_a_response_cannot_be_regressed_by_opening_the_link_again(
    db_session, alert, client, audit_sink
):
    """ADR-014 through the donor path: OPENED must not pull CONFIRMED back."""
    raw = issue_token(db_session, alert, 120)
    client.post(f"/d/{raw}", data={"response": "confirm"})

    client.get(f"/d/{raw}")

    state, rank = db_session.execute(
        text("SELECT state, state_rank FROM core.alert WHERE id = :i"), {"i": alert.id}
    ).first()
    assert (state, rank) == ("CONFIRMED", 4)


def test_responding_emits_an_audit_event_with_no_token_in_it(
    db_session, alert, client, audit_sink
):
    raw = issue_token(db_session, alert, 120)

    client.post(f"/d/{raw}", data={"response": "confirm"})

    rows = audit_sink()
    assert [r.action for r in rows] == ["DONOR_CONFIRMED"]
    assert raw not in str(rows[0].detail)


# --- the QR endpoint -------------------------------------------------------


def test_qr_endpoint_returns_a_png(db_session, alert, client):
    raw = issue_token(db_session, alert, 120)

    response = client.get(f"/d/{raw}/qr")

    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


# --- ADR-016 isolation -----------------------------------------------------


def test_the_donor_app_has_no_import_path_to_matching_or_readmodel():
    """Requirement 14, the import half. The database half is the bga_donor
    role, which holds no grant at all on core.donor or core.outbox.

    Run in a subprocess: measuring what a fresh import pulls in requires a
    clean sys.modules, and clearing it in-process resets every already-imported
    bga module and breaks the rest of the run.
    """
    import subprocess
    import sys
    from pathlib import Path

    from bga.config import settings

    project_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, bga.api.donor_main;"
            "print('\\n'.join(m for m in sys.modules if m.startswith('bga.')))",
        ],
        capture_output=True,
        text=True,
        cwd=str(project_root),
        env={
            **os.environ,
            "DATABASE_URL_DONOR": settings.database_url_donor,
        },
    )
    assert result.returncode == 0, result.stderr

    loaded = set(result.stdout.split())
    assert "bga.domain.matching" not in loaded
    assert "bga.domain.readmodel" not in loaded
    assert "bga.api.dashboard" not in loaded
    assert "bga.api.receipts" not in loaded


def test_the_donor_app_is_a_different_application_from_the_clinician_one():
    from bga.api.donor_main import app as donor
    from bga.api.main import app as clinician

    assert donor is not clinician
    donor_paths = {r.path for r in donor.routes if hasattr(r, "path")}
    assert not any(p.startswith("/webhooks") for p in donor_paths)


def test_the_donor_app_publishes_no_openapi_schema():
    """A public origin should not advertise its surface."""
    assert donor_app.openapi_url is None
