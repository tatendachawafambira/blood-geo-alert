"""Deficit requests, alerts, tokens and the dispatch outbox.

core.deficit_request and core.alert are the two tables under row-level
security (ADR-017). The migration attaches the policies; the models only
declare the shape.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bga.db import Base

# Roles are plain strings for now. Login is not implemented today, but the
# tables exist so that adding it later is a feature, not a migration of live
# production data.
ROLES = (
    "CLINICIAN",
    "COLLECTION_SUPERVISOR",
    "BRANCH_ADMIN",
    "SYSTEM_ADMIN",
    "DPO_AUDITOR",
)

URGENCIES = ("ROUTINE", "URGENT", "CRITICAL")

REQUEST_STATES = (
    "DRAFT",
    "MATCHING",
    "ACTIVE",
    "ESCALATED",
    "FULFILLED",
    "EXPIRED",
    "REJECTED_NO_POINT",
    "NO_CANDIDATES",
    "CANCELLED",
    "CLOSED",
)

OUTBOX_STATES = ("QUEUED", "LOCKED", "SENT", "SUPPRESSED", "DEAD")

# ADR-014. A transition applies only where it strictly advances the rank, so a
# late DELIVERED receipt cannot pull a CONFIRMED alert backwards.
ALERT_STATE_RANKS = {
    "QUEUED": 0,
    "SENT": 1,
    "DELIVERED": 2,
    "OPENED": 3,
    "CONFIRMED": 4,
    "DECLINED": 4,
    "SUPPRESSED": 5,
    "DEAD": 5,
}


class AppUser(Base):
    __tablename__ = "app_user"
    __table_args__ = (
        sa.CheckConstraint(
            "role IN ('CLINICIAN', 'COLLECTION_SUPERVISOR', 'BRANCH_ADMIN',"
            " 'SYSTEM_ADMIN', 'DPO_AUDITOR')",
            name="ck_app_user_role",
        ),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    facility_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("core.facility.id"), index=True
    )
    display_name: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    role: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )


class DeficitRequest(Base):
    """One mobilisation event. Row-level security scopes it to its facility."""

    __tablename__ = "deficit_request"
    __table_args__ = (
        sa.CheckConstraint("abo IN ('O', 'A', 'B', 'AB')", name="ck_deficit_request_abo"),
        sa.CheckConstraint("rh IN ('POS', 'NEG')", name="ck_deficit_request_rh"),
        sa.CheckConstraint(
            "urgency IN ('ROUTINE', 'URGENT', 'CRITICAL')", name="ck_deficit_request_urgency"
        ),
        sa.CheckConstraint("units_required > 0", name="ck_deficit_request_units"),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    facility_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("core.facility.id"), nullable=False, index=True
    )
    # NULL when no point was open: the request is recorded and rejected, and
    # nothing is dispatched (FR-COL-04).
    collection_point_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("core.collection_point.id")
    )

    abo: Mapped[str] = mapped_column(sa.String(2), nullable=False)
    rh: Mapped[str] = mapped_column(sa.String(3), nullable=False)
    units_required: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    urgency: Mapped[str] = mapped_column(sa.String(10), nullable=False)
    radius_metres: Mapped[int | None] = mapped_column(sa.Integer)

    state: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(sa.Text)
    matched_stats: Mapped[dict | None] = mapped_column(JSONB)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("core.app_user.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Alert(Base):
    """One outbound contact to one donor for one request."""

    __tablename__ = "alert"
    __table_args__ = (
        # A database-level guarantee against duplicate dispatch, independent of
        # any application logic.
        sa.UniqueConstraint("deficit_request_id", "donor_id", name="uq_alert_request_donor"),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    deficit_request_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("core.deficit_request.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    donor_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("core.donor.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, server_default="SMS"
    )
    # Indexed because delivery receipts arrive keyed on this, under burst.
    provider_message_id: Mapped[str | None] = mapped_column(sa.String(128), index=True)

    state: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, server_default="QUEUED"
    )
    state_rank: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    # FR-MSG-03: no blood group, no name, no patient or clinical detail.
    message_body: Mapped[str | None] = mapped_column(sa.Text)

    queued_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ResponseToken(Base):
    """Only the SHA-256 hash is stored. The raw token exists in the SMS only."""

    __tablename__ = "response_token"
    __table_args__ = {"schema": "core"}

    token_hash: Mapped[bytes] = mapped_column(sa.LargeBinary, primary_key=True)
    alert_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("core.alert.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class Outbox(Base):
    """The queue. ADR-013 - Postgres is the broker in Release 1.0.

    Written in the SAME transaction as the alert rows (ADR-010), which is what
    removes the choice between losing messages and sending phantom ones.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        sa.CheckConstraint(
            "state IN ('QUEUED', 'LOCKED', 'SENT', 'SUPPRESSED', 'DEAD')",
            name="ck_outbox_state",
        ),
        # Supports the SKIP LOCKED drain without scanning completed rows.
        sa.Index("ix_outbox_drain", "state", "next_attempt_at"),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    alert_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("core.alert.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    state: Mapped[str] = mapped_column(
        sa.String(12), nullable=False, server_default="QUEUED"
    )
    locked_by: Mapped[str | None] = mapped_column(sa.String(64))
    locked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.Text)


class DeadLetter(Base):
    """Exhausted retries land here. Nothing is ever silently dropped (P-04)."""

    __tablename__ = "dead_letter"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    outbox_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
