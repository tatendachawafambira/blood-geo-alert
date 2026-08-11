"""Request-scoped dependencies for the clinician application.

Every dashboard query runs inside a scoped session, so row-level security is
applied by the database on every read - not only on the ones somebody
remembered to filter (ADR-017).

Facility selection stands in for login. Release 1.0 does not implement
authentication, but the app_user and role tables exist, so adding it later is a
feature rather than a migration of live data. The selected facility is carried
in a cookie: the redirect after POST /requests has to land somewhere that can
still scope its queries, and putting the id in the URL would leak it into
referrer headers, proxy logs and browser history.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from bga.db import scoped_session

FACILITY_COOKIE = "bga_facility"

# Not authentication. Named so that nobody mistakes it for authentication.
DASHBOARD_ACTOR = "dashboard"
DASHBOARD_ROLE = "CLINICIAN"


def selected_facility_id(request: Request) -> UUID | None:
    """The facility this browser last chose, or None."""
    raw = request.cookies.get(FACILITY_COOKIE)
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def get_scoped_session(request: Request) -> Iterator[Session]:
    """Yield a session with app.facility_id set for the whole transaction.

    Refuses rather than falling back to an unscoped session. An unscoped
    session does not error - RLS simply returns nothing - so a silent fallback
    would render a page of zeroes that looks like a quiet afternoon.
    """
    facility_id = selected_facility_id(request)
    if facility_id is None:
        raise HTTPException(
            status_code=400,
            detail="No facility selected. Return to the dashboard and raise a request.",
        )

    with scoped_session(
        facility_id=facility_id, actor_id=DASHBOARD_ACTOR, role=DASHBOARD_ROLE
    ) as session:
        yield session


def unscoped_session() -> Iterator[Session]:
    """For the landing form only, which lists facilities before one is chosen.

    core.facility carries no row-level security - it is reference data, not
    request data. Nothing that reads deficit_request or alert may use this.
    """
    with scoped_session(facility_id=None, actor_id=DASHBOARD_ACTOR, role=DASHBOARD_ROLE) as s:
        yield s
