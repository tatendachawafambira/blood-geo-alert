"""Consent capture, withdrawal and suppression.

The consent text below is a legal artefact, not interface copy. It is written
against the disclosure checklist in the Cyber and Data Protection Act section
15, carried into this project as CR-04 and FR-CON-02, and each language states
all seven required elements:

  1. who the controller is                    - NBSZ, named
  2. what is held                             - mobile number, blood group,
                                                suburb, donation eligibility
  3. the purpose                              - contacting you when blood is
                                                needed near you
  4. the categories of recipient              - NBSZ staff, the mobile network
                                                that delivers the message
  5. whether a reply is required, and the
     consequence of not replying              - it is optional, nothing happens
  6. withdrawal, free of charge, at any time  - reply STOP
  7. the rights of access and correction      - stated

TRANSLATION STATUS: the Shona and Ndebele texts are working translations of
the approved English. They must be reviewed by a first-language speaker and
signed off by the Data Protection Officer before any real donor is enrolled.
Consent captured against unreviewed text in a language the donor actually
reads is not obviously valid consent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from bga.domain import audit
from bga.models.donor import ConsentRecord, Donor, SuppressionRecord

CONSENT_VERSION = "1.0"

CONSENT_TEXT = {
    "en": (
        "National Blood Service Zimbabwe (NBSZ) is the controller of your "
        "information. We hold your mobile number, blood group, suburb and "
        "donation eligibility. We use them only to contact you when blood is "
        "needed near you. Your details are seen by NBSZ staff and by the mobile "
        "network that delivers the message; they are never sold or used for "
        "anything else. Replying is your choice - if you do not reply, nothing "
        "happens and you stay registered. You may withdraw at any time, free of "
        "charge, by replying STOP. You may ask to see the information we hold "
        "about you and to have it corrected."
    ),
    "sn": (
        "Sangano reNational Blood Service Zimbabwe (NBSZ) ndiro rinochengeta "
        "ruzivo rwenyu. Tinochengeta nhamba yenyu yerunhare, mhando yeropa "
        "renyu, nzvimbo yamunogara, uye kukodzera kwenyu kupa ropa. "
        "Tinozvishandisa chete kuti tikubatei kana ropa richidiwa pedyo nemi. "
        "Ruzivo rwenyu runoonekwa nevashandi veNBSZ nekambani yerunhare "
        "inotumira meseji; haruna kutengeswa kana kushandiswa pane zvimwe. "
        "Kupindura kusarudza kwenyu - kana musingapinduri, hapana chinoitika "
        "uye munoramba makanyoreswa. Munogona kubuda chero nguva, pasina "
        "muripo, nekupindura kuti STOP. Munogona kukumbira kuona ruzivo "
        "rwatinarwo uye kuti rugadziriswe."
    ),
    "nd": (
        "INational Blood Service Zimbabwe (NBSZ) yiyo ephethe imininingwane "
        "yakho. Sigcina inombolo yakho yomakhalekhukhwini, uhlobo lwegazi "
        "lakho, indawo ohlala kuyo, kanye lokufaneleka kwakho ukunikela igazi. "
        "Sikusebenzisa kuphela ukuthi sikuthinte nxa igazi lidingeka eduze "
        "lawe. Imininingwane yakho ibonwa yizisebenzi zeNBSZ lenkampani "
        "yomakhalekhukhwini ethumela umlayezo; kayithengiswa njalo "
        "kayisetshenziselwa okunye. Ukuphendula yikuzikhethela kwakho - nxa "
        "ungaphenduli, akulalutho olwenzakalayo njalo uhlala ubhalisiwe. "
        "Ungayekela nini lanini, mahala, ngokuphendula uthi STOP. Ungacela "
        "ukubona imininingwane esilayo lokuthi ilungiswe."
    ),
}


def _suppression_hash(blind_index: bytes) -> bytes:
    """SHA-256 of the blind index, which is what survives erasure (FR-CON-08).

    A further hash rather than the blind index itself. After erasure the donor
    row is gone; keeping the raw blind index would leave the suppression table
    as a working copy of the lookup index for every person who ever withdrew,
    which is the opposite of what erasure is for. This value is enough to
    recognise the same number at the next enrolment drive and nothing else.
    """
    return sha256(blind_index).digest()


def grant_consent(session: Session, donor: Donor, channel: str, language: str) -> ConsentRecord:
    """Record consent, storing the verbatim text the donor was shown.

    The text is copied into the row rather than referenced by version, because
    a subject access request years later must reproduce what this person
    actually saw, not what version 1.0 says today.
    """
    if language not in CONSENT_TEXT:
        # No silent fall back to English. Consent given against text in a
        # language the donor may not read is not informed consent.
        raise ValueError(f"no consent text for language {language!r}")

    record = ConsentRecord(
        donor_id=donor.id,
        consent_version=CONSENT_VERSION,
        granted_at=datetime.now(timezone.utc),
        channel=channel,
        text_presented=CONSENT_TEXT[language],
    )
    session.add(record)
    session.flush()

    audit.emit(
        action="CONSENT_GRANTED",
        actor=f"donor:{donor.id}",
        target_class="Donor",
        target_id=donor.id,
        detail={"consent_version": CONSENT_VERSION, "channel": channel, "language": language},
    )
    return record


def withdraw_consent(session: Session, donor: Donor, channel: str) -> None:
    """Stop all contact with this donor, permanently and immediately.

    Everything below happens in the caller's single transaction: the opt-out
    stamp, closing the consent record, and the suppression row either all land
    or none do. There is no state where a donor is half-withdrawn.

    The audit event is the one thing outside that transaction, by design - it
    is written on the audit role's own connection (see domain/audit.py). The
    consequence is that a rolled-back withdrawal can leave an audit row saying
    it happened. That is the right way round: an audit record of an attempted
    withdrawal is evidence in the donor's favour, whereas a missing one leaves
    a rights request with nothing to show.
    """
    now = datetime.now(timezone.utc)

    # Keep the earliest withdrawal time. Re-sending STOP must be harmless, and
    # the first request is the one that matters for FR-CON-04's sixty seconds.
    if donor.opt_out_at is None:
        donor.opt_out_at = now

    session.execute(
        update(ConsentRecord)
        .where(ConsentRecord.donor_id == donor.id, ConsentRecord.withdrawn_at.is_(None))
        .values(withdrawn_at=now)
    )

    # ON CONFLICT DO NOTHING: withdrawing twice is not an error.
    session.execute(
        pg_insert(SuppressionRecord)
        .values(msisdn_hash=_suppression_hash(donor.msisdn_blind_index), created_at=now)
        .on_conflict_do_nothing(index_elements=["msisdn_hash"])
    )

    session.flush()

    audit.emit(
        action="CONSENT_WITHDRAWN",
        actor=f"donor:{donor.id}",
        target_class="Donor",
        target_id=donor.id,
        detail={"channel": channel},
    )


def is_suppressed(session: Session, blind_idx: bytes) -> bool:
    """True if this number has previously withdrawn, even after erasure.

    Checked at enrolment so that someone who asked not to be contacted is not
    silently re-registered at the next drive.
    """
    return (
        session.execute(
            select(SuppressionRecord.msisdn_hash).where(
                SuppressionRecord.msisdn_hash == _suppression_hash(blind_idx)
            )
        ).first()
        is not None
    )
