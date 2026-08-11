"""The donor register: the most sensitive data in the system.

The MSISDN is stored twice and never in plaintext (ADR-012):
  msisdn_ciphertext   AES-256-GCM, random nonce  -> used when dispatching
  msisdn_blind_index  HMAC-SHA256 under a pepper -> used for lookup and dedup

Both are produced in the application by crypto/field.py. The database never
holds key material, so a database backup is not a decryptable artefact.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from bga.db import Base

ABO_GROUPS = ("O", "A", "B", "AB")
RH_TYPES = ("POS", "NEG")
SEXES = ("M", "F")
VERIFICATION_STATES = ("SELF_DECLARED", "NBSZ_VERIFIED")
AVAILABILITY_STATES = ("AVAILABLE", "PAUSED")
LANGUAGES = ("en", "sn", "nd")
AGE_BANDS = ("UNDER_18", "EIGHTEEN_PLUS")


class Donor(Base):
    __tablename__ = "donor"
    __table_args__ = (
        sa.CheckConstraint("abo IN ('O', 'A', 'B', 'AB')", name="ck_donor_abo"),
        sa.CheckConstraint("rh IN ('POS', 'NEG')", name="ck_donor_rh"),
        sa.CheckConstraint("sex IN ('M', 'F')", name="ck_donor_sex"),
        sa.CheckConstraint(
            "verification_state IN ('SELF_DECLARED', 'NBSZ_VERIFIED')",
            name="ck_donor_verification_state",
        ),
        sa.CheckConstraint(
            "availability IN ('AVAILABLE', 'PAUSED')", name="ck_donor_availability"
        ),
        sa.CheckConstraint(
            "preferred_language IN ('en', 'sn', 'nd')", name="ck_donor_language"
        ),
        sa.CheckConstraint(
            "age_band IN ('UNDER_18', 'EIGHTEEN_PLUS')", name="ck_donor_age_band"
        ),
        # Partial: opted-out and paused donors are not in the index at all.
        # Declared here so autogenerate does not later propose dropping it.
        sa.Index(
            "ix_donor_ward_group",
            "ward_id",
            "abo",
            "rh",
            postgresql_where=sa.text("opt_out_at IS NULL AND availability = 'AVAILABLE'"),
        ),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)

    msisdn_ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    # UNIQUE is what enforces FR-DON-07 deduplication without ever decrypting.
    msisdn_blind_index: Mapped[bytes] = mapped_column(
        sa.LargeBinary, nullable=False, unique=True
    )
    # The NBSZ donor number, when the blood service has supplied one.
    donor_reference: Mapped[str | None] = mapped_column(sa.String(64), index=True)

    abo: Mapped[str] = mapped_column(sa.String(2), nullable=False)
    rh: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    sex: Mapped[str] = mapped_column(sa.String(1), nullable=False)

    ward_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("core.ward.id"), nullable=False
    )

    verification_state: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, server_default="SELF_DECLARED"
    )
    availability: Mapped[str] = mapped_column(
        sa.String(10), nullable=False, server_default="AVAILABLE"
    )
    preferred_language: Mapped[str] = mapped_column(
        sa.String(2), nullable=False, server_default="en"
    )
    age_band: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    guardian_consent_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid)

    # The frequency cap reads and writes this column atomically. ADR-015.
    last_alerted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    opt_out_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


class DonorEligibility(Base):
    """Derived eligibility state. One row per donor, always.

    next_eligible_date is NOT NULL by design. The reservation query compares
    e.next_eligible_date <= :today, and a NULL there makes the whole predicate
    NULL rather than true - the donor silently vanishes from every match.
    Never-donated donors get a past date, not NULL.
    """

    __tablename__ = "donor_eligibility"
    __table_args__ = {"schema": "core"}

    donor_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("core.donor.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_donation_date: Mapped[date | None] = mapped_column(sa.Date)
    next_eligible_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    deferral_flag: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    deferral_review_date: Mapped[date | None] = mapped_column(sa.Date)
    donations_last_12m: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0"
    )


class ConsentRecord(Base):
    """Immutable record of a consent grant. Withdrawal closes it (FR-CON-01)."""

    __tablename__ = "consent_record"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    donor_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("core.donor.id", ondelete="CASCADE"), nullable=False, index=True
    )
    consent_version: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    # The verbatim text shown to the donor, retained for every version.
    text_presented: Mapped[str] = mapped_column(sa.Text, nullable=False)


class SuppressionRecord(Base):
    """Survives erasure. FR-CON-08.

    After a donor's personal data is erased, this hash is the only thing left -
    just enough to recognise the number at the next enrolment drive and refuse
    to silently re-register someone who asked not to be contacted.
    """

    __tablename__ = "suppression_record"
    __table_args__ = {"schema": "core"}

    msisdn_hash: Mapped[bytes] = mapped_column(sa.LargeBinary, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
