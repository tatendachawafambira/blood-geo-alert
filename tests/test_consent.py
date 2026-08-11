"""Consent capture, withdrawal, suppression, and the consent text itself.

The text tests are not decoration. CONSENT_TEXT is a legal artefact assessed
against section 15 of the Cyber and Data Protection Act (CR-04, FR-CON-02), and
an edit that drops a required disclosure is not otherwise detectable.
"""

from __future__ import annotations

import os
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import cast, func, text

from bga.crypto.field import blind_index, encrypt_msisdn
from bga.domain.consent import (
    CONSENT_TEXT,
    CONSENT_VERSION,
    grant_consent,
    is_suppressed,
    withdraw_consent,
)
from bga.models.donor import ConsentRecord, Donor
from bga.models.reference import Geography, Ward

NUMBER = "+263771234567"
LANGUAGES = ("en", "sn", "nd")


@pytest.fixture()
def donor(db_session) -> Donor:
    ward = Ward(
        name=f"ward-{uuid4().hex[:8]}",
        centroid=cast(func.ST_SetSRID(func.ST_MakePoint(31.05, -17.83), 4326), Geography()),
    )
    db_session.add(ward)
    db_session.flush()

    record = Donor(
        msisdn_ciphertext=encrypt_msisdn(NUMBER),
        msisdn_blind_index=blind_index(NUMBER),
        abo="O",
        rh="POS",
        sex="M",
        ward_id=ward.id,
        verification_state="NBSZ_VERIFIED",
        availability="AVAILABLE",
        preferred_language="sn",
        age_band="EIGHTEEN_PLUS",
    )
    db_session.add(record)
    db_session.flush()
    return record


# --- the consent text (CR-04 / FR-CON-02 / Act s.15) -----------------------


def test_all_three_languages_exist():
    assert set(CONSENT_TEXT) == set(LANGUAGES)


@pytest.mark.parametrize("language", LANGUAGES)
def test_each_text_is_under_seven_hundred_characters(language):
    assert 0 < len(CONSENT_TEXT[language]) < 700


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_controller_is_named(language):
    assert "NBSZ" in CONSENT_TEXT[language]


@pytest.mark.parametrize("language", LANGUAGES)
def test_withdrawal_keyword_is_stated(language):
    assert "STOP" in CONSENT_TEXT[language]


# Phrase by phrase, per language. Written out rather than generated: this is
# the section 15 checklist, and it should be legible to a reviewer who does not
# read Python.
DISCLOSURES = {
    "en": [
        "mobile number", "blood group", "suburb", "donation eligibility",
        "contact you when blood is", "NBSZ staff", "mobile network",
        "your choice", "nothing", "free of", "any time",
        "see the information", "corrected",
    ],
    "sn": [
        "nhamba yenyu yerunhare", "mhando yeropa", "nzvimbo yamunogara",
        "kukodzera kwenyu kupa ropa", "tikubatei kana ropa richidiwa",
        "vashandi veNBSZ", "kambani yerunhare", "kusarudza kwenyu",
        "hapana chinoitika", "pasina", "chero nguva",
        "kuona ruzivo", "rugadziriswe",
    ],
    "nd": [
        "inombolo yakho yomakhalekhukhwini", "uhlobo lwegazi", "indawo ohlala kuyo",
        "lokufaneleka kwakho ukunikela igazi", "sikuthinte nxa igazi lidingeka",
        "izisebenzi zeNBSZ", "lenkampani yomakhalekhukhwini", "yikuzikhethela",
        "akulalutho", "mahala", "nini lanini",
        "ukubona imininingwane", "ilungiswe",
    ],
}


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_required_disclosure_is_present(language):
    text_body = CONSENT_TEXT[language].lower()
    missing = [p for p in DISCLOSURES[language] if p.lower() not in text_body]
    assert not missing, f"{language} consent text is missing: {missing}"


# --- granting --------------------------------------------------------------


def test_grant_consent_stores_the_verbatim_text_shown(db_session, donor, audit_sink):
    record = grant_consent(db_session, donor, channel="USSD", language="sn")

    assert record.text_presented == CONSENT_TEXT["sn"]
    assert record.consent_version == CONSENT_VERSION
    assert record.channel == "USSD"
    assert record.withdrawn_at is None


def test_grant_consent_emits_an_audit_event(db_session, donor, audit_sink):
    grant_consent(db_session, donor, channel="WEB", language="en")

    rows = audit_sink()
    assert [r.action for r in rows] == ["CONSENT_GRANTED"]
    assert rows[0].detail["language"] == "en"
    assert rows[0].detail["consent_version"] == CONSENT_VERSION


def test_an_unsupported_language_raises_rather_than_falling_back(db_session, donor):
    """Consent given against text in a language the donor may not read is not
    informed consent, so there is no silent fall back to English."""
    with pytest.raises(ValueError, match="no consent text"):
        grant_consent(db_session, donor, channel="WEB", language="fr")


# --- withdrawal ------------------------------------------------------------


def test_withdrawal_stops_all_contact(db_session, donor, audit_sink):
    grant_consent(db_session, donor, channel="USSD", language="sn")

    withdraw_consent(db_session, donor, channel="SMS")

    assert donor.opt_out_at is not None


def test_withdrawal_closes_the_open_consent_record(db_session, donor, audit_sink):
    grant_consent(db_session, donor, channel="USSD", language="sn")

    withdraw_consent(db_session, donor, channel="SMS")

    record = db_session.query(ConsentRecord).filter_by(donor_id=donor.id).one()
    assert record.withdrawn_at is not None


def test_withdrawal_writes_a_suppression_record(db_session, donor, audit_sink):
    withdraw_consent(db_session, donor, channel="SMS")

    assert is_suppressed(db_session, donor.msisdn_blind_index) is True


def test_withdrawal_emits_an_audit_event(db_session, donor, audit_sink):
    withdraw_consent(db_session, donor, channel="SMS")

    assert [r.action for r in audit_sink()] == ["CONSENT_WITHDRAWN"]


def test_withdrawing_twice_is_harmless(db_session, donor, audit_sink):
    """Re-sending STOP must not error and must not duplicate the suppression."""
    withdraw_consent(db_session, donor, channel="SMS")
    first_time = donor.opt_out_at

    withdraw_consent(db_session, donor, channel="USSD")

    assert donor.opt_out_at == first_time, "the first withdrawal is the one that counts"
    count = db_session.execute(
        text("SELECT count(*) FROM core.suppression_record")
    ).scalar()
    assert count == 1


def test_withdrawal_works_without_a_prior_consent_record(db_session, donor, audit_sink):
    withdraw_consent(db_session, donor, channel="SMS")

    assert donor.opt_out_at is not None
    assert is_suppressed(db_session, donor.msisdn_blind_index) is True


# --- suppression -----------------------------------------------------------


def test_a_number_is_not_suppressed_before_withdrawal(db_session, donor):
    assert is_suppressed(db_session, donor.msisdn_blind_index) is False


def test_an_unrelated_number_is_not_suppressed(db_session, donor, audit_sink):
    withdraw_consent(db_session, donor, channel="SMS")

    assert is_suppressed(db_session, blind_index("+263772234567")) is False


def test_the_suppression_row_is_a_hash_of_the_blind_index_not_the_index(
    db_session, donor, audit_sink
):
    """FR-CON-08. After erasure this row is all that remains; storing the blind
    index itself would leave a working copy of the lookup key for everyone who
    ever withdrew."""
    withdraw_consent(db_session, donor, channel="SMS")

    stored = db_session.execute(text("SELECT msisdn_hash FROM core.suppression_record")).scalar()
    assert bytes(stored) == sha256(donor.msisdn_blind_index).digest()
    assert bytes(stored) != donor.msisdn_blind_index


def test_suppression_survives_the_donor_being_erased(db_session, donor, audit_sink):
    """The whole point of FR-CON-08: recognise the number at the next drive."""
    withdraw_consent(db_session, donor, channel="SMS")
    saved_index = bytes(donor.msisdn_blind_index)

    db_session.delete(donor)
    db_session.flush()

    assert db_session.execute(text("SELECT count(*) FROM core.donor")).scalar() == 0
    assert is_suppressed(db_session, saved_index) is True


def test_no_msisdn_reaches_the_audit_trail(db_session, donor, audit_sink):
    grant_consent(db_session, donor, channel="USSD", language="sn")
    withdraw_consent(db_session, donor, channel="SMS")

    assert "263" not in str([r.detail for r in audit_sink()])
