"""Reference data: wards, collection points, facilities, and the precomputed
ward-to-collection-point proximity table (ADR-011).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from bga.db import Base

COLLECTION_POINT_KINDS = ("BRANCH", "STATIC_CLINIC", "MOBILE")


class Geography(UserDefinedType):
    """PostGIS geography(Point,4326), without the geoalchemy2 dependency.

    Geography, not geometry: geography computes distance on the spheroid in
    METRES, which is what "a 3 km radius" actually means. Geometry in SRID 4326
    would compute in degrees and be silently wrong at Zimbabwean latitudes
    (SAD 7.3.1).

    This type exists so the column is present in Base.metadata. Values are only
    ever read and written by the raw PostGIS SQL in domain/proximity.py and
    domain/collection.py, so no bind/result processing is needed here.
    """

    cache_ok = True

    def __init__(self, geometry_type: str = "Point", srid: int = 4326) -> None:
        self.geometry_type = geometry_type
        self.srid = srid

    def get_col_spec(self, **kw: object) -> str:
        return f"geography({self.geometry_type},{self.srid})"


class Ward(Base):
    """A suburb or ward. Its centroid is the ONLY location a donor has.

    ADR-004: donors carry no device position. Every donor in a ward shares this
    one coordinate, which is what makes ADR-011's precompute possible at all.
    """

    __tablename__ = "ward"
    __table_args__ = (
        # Declared here, not only in the migration: an index that exists in the
        # database but not in Base.metadata is one that the next autogenerate
        # will quietly emit a DROP for.
        sa.Index("ix_ward_centroid_gist", "centroid", postgresql_using="gist"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.String(120), nullable=False, unique=True)
    district: Mapped[str | None] = mapped_column(sa.String(120))
    centroid: Mapped[object] = mapped_column(Geography("Point", 4326), nullable=False)


class CollectionPoint(Base):
    """Somewhere blood may lawfully be collected. NOT a hospital (ADR-001)."""

    __tablename__ = "collection_point"
    __table_args__ = (
        sa.CheckConstraint(
            "kind IN ('BRANCH', 'STATIC_CLINIC', 'MOBILE')",
            name="ck_collection_point_kind",
        ),
        sa.CheckConstraint(
            "open_hour >= 0 AND open_hour < 24 AND close_hour > 0 AND close_hour <= 24"
            " AND open_hour < close_hour",
            name="ck_collection_point_hours",
        ),
        sa.Index("ix_collection_point_location_gist", "location", postgresql_using="gist"),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    location: Mapped[object] = mapped_column(Geography("Point", 4326), nullable=False)

    open_hour: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    close_hour: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    open_saturday: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )
    open_sunday: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    # Mobile drives are time-bounded. NULL means permanent (FR-COL-02).
    active_from: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    active_to: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true()
    )


class Facility(Base):
    """A hospital that raises deficit requests. Donors are never sent here."""

    __tablename__ = "facility"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(160), nullable=False, unique=True)
    latitude: Mapped[float] = mapped_column(sa.Float, nullable=False)
    longitude: Mapped[float] = mapped_column(sa.Float, nullable=False)
    contact: Mapped[str | None] = mapped_column(sa.String(120))


class WardCollectionProximity(Base):
    """Precomputed ward-to-collection-point distances. ADR-011.

    The primary key order (collection_point_id, metres, ward_id) is the whole
    point of this table. The runtime matching query filters
        p.collection_point_id = :cp_id AND p.metres <= :radius
    which that key order answers as a single index range scan, before any donor
    row is touched. Reordering these columns silently turns the hot path into a
    filter over the whole catchment.
    """

    __tablename__ = "ward_collection_proximity"
    __table_args__ = (
        sa.PrimaryKeyConstraint(
            "collection_point_id", "metres", "ward_id", name="pk_ward_collection_proximity"
        ),
        {"schema": "core"},
    )

    ward_id: Mapped[int] = mapped_column(
        sa.Integer, sa.ForeignKey("core.ward.id", ondelete="CASCADE"), nullable=False
    )
    collection_point_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("core.collection_point.id", ondelete="CASCADE"),
        nullable=False,
    )
    # double precision: this is what ST_Distance returns, stored unrounded.
    metres: Mapped[float] = mapped_column(sa.Float, nullable=False)
