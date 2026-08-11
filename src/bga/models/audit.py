"""The audit store. Schema "audit", written by the bga_audit role only.

Separated from core so that compromise of the application cannot rewrite the
record of that compromise (SAD 5.1). bga_app holds no grant on this schema at
all - not SELECT, not UPDATE, not DELETE.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bga.db import Base


class AuditEvent(Base):
    """Append-only, hash-chained.

    id is an identity column rather than a UUID because the hash chain needs a
    total order: prev_hash is the entry_hash of the highest id. A random UUID
    gives no ordering to chain along.
    """

    __tablename__ = "audit_event"
    __table_args__ = {"schema": "audit"}

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=False), primary_key=True)
    actor: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    target_class: Mapped[str | None] = mapped_column(sa.String(64))
    target_id: Mapped[str | None] = mapped_column(sa.String(64))
    # Never an MSISDN, a token, or any decrypted value.
    detail: Mapped[dict | None] = mapped_column(JSONB)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, index=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    prev_hash: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    entry_hash: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
