"""Ward-to-collection-point proximity precompute. ADR-011.

This module is the ONLY place in the system where a spatial operator appears.
Every donor shares their ward's centroid, so the register holds a few thousand
distinct coordinates however many donors it contains - and collection points
are static too. The whole distance relation is therefore computable in advance,
which removes PostGIS from the hot path entirely and turns runtime matching
into an indexed integer join.

If you find ST_ anywhere under domain/ other than here and collection.py's
nearest-open-point lookup, something has drifted from the architecture.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

# The two statements are issued separately because they are two statements -
# psycopg cannot run a multi-statement string with bound parameters. Each is
# used exactly as specified.
#
# The one edit: the cast colons are written \:\: inside raw strings. text()
# scans for :name bind parameters and cannot tell a PostgreSQL cast from one,
# so an unescaped :cp_id::uuid is parsed as the parameter ":cp_id:" and passed
# through to the server raw, which fails with 'syntax error at or near ":"'.
# The escape is consumed by text(); the SQL Postgres receives is unchanged.
_DELETE_PROXIMITY = text(
    r"""
    DELETE FROM core.ward_collection_proximity
     WHERE (:cp_id\:\:uuid IS NULL OR collection_point_id = :cp_id)
    """
)

_INSERT_PROXIMITY = text(
    r"""
    INSERT INTO core.ward_collection_proximity (ward_id, collection_point_id, metres)
    SELECT w.id, c.id, ST_Distance(w.centroid, c.location)
      FROM core.ward w
      CROSS JOIN core.collection_point c
     WHERE (:cp_id\:\:uuid IS NULL OR c.id = :cp_id)
       AND ST_DWithin(w.centroid, c.location, :max_metres)
    """
)

# ST_DWithin, not a comparison against ST_Distance: ST_DWithin can be answered
# from the GiST index, while a predicate over ST_Distance forces every
# candidate pair to be evaluated.

_IS_STALE = text(
    """
    SELECT EXISTS (
        SELECT 1
          FROM core.collection_point c
         WHERE c.is_active
           AND NOT EXISTS (
               SELECT 1
                 FROM core.ward_collection_proximity p
                WHERE p.collection_point_id = c.id))
    """
)


def rebuild_proximity(
    session: Session,
    max_metres: int = 15000,
    collection_point_id: UUID | None = None,
) -> int:
    """Recompute the proximity rows, and return how many were inserted.

    Pass collection_point_id to rebuild one point - which is what a collection
    point lifecycle event does, since a mobile drive activating must refresh
    synchronously rather than waiting for a timer. Pass None to rebuild all.

    Delete and insert run in the caller's transaction, so the table is never
    observed half-rebuilt. This function does not commit; the caller owns that.
    """
    params = {"cp_id": collection_point_id, "max_metres": max_metres}

    session.execute(_DELETE_PROXIMITY, params)
    result = session.execute(_INSERT_PROXIMITY, params)
    return result.rowcount


def proximity_is_stale(session: Session) -> bool:
    """True if any active collection point has no proximity rows at all.

    Guards ARK-08. A stale proximity table does not raise, it silently returns
    the wrong catchment - donors near the point are simply never matched, and
    the dashboard reports a small eligible count that looks plausible. Silent
    wrongness is the worst failure class in this system, so the condition is
    checked rather than assumed.

    Note that an active point genuinely having no wards within max_metres is
    indistinguishable from a never-computed one. That is the conservative
    reading and it is the right way round: the alternative is to stay quiet
    about a point that has never been computed.
    """
    return bool(session.execute(_IS_STALE).scalar())
