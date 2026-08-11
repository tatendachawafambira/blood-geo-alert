"""Clinician dashboard: raise a deficit request, then watch it.

The whole request flow is ONE transaction (ADR-010). Resolving the collection
point, reserving contact, creating alerts and writing the outbox either all
land or none do - there is no state in which donors are marked as contacted but
no message was ever queued for them.

Two things this page is careful about:

  It reports the RESERVED count, not the matched count. Under concurrency they
  differ, and the reserved figure is the one that corresponds to messages
  (ADR-015). Card 2 says so on screen rather than leaving it to be discovered.

  It never renders an individual. Everything on the detail page comes from
  domain/readmodel.py, which cannot return a donor row.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from bga.api.deps import FACILITY_COOKIE, get_scoped_session, unscoped_session
from bga.config import settings
from bga.db import scoped_session
from bga.domain import audit, readmodel
from bga.domain.collection import resolve_collection_point
from bga.domain.dispatch import create_alerts_and_outbox
from bga.domain.matching import match_and_reserve
from bga.models.reference import Facility
from bga.models.requests import DeficitRequest

log = structlog.get_logger("bga.dashboard")

router = APIRouter(tags=["dashboard"])

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

HARARE = ZoneInfo("Africa/Harare")

BLOOD_GROUPS = ("O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-")
URGENCIES = ("ROUTINE", "URGENT", "CRITICAL")


def _split_group(group: str) -> tuple[str, str]:
    """'AB-' -> ('AB', 'NEG')."""
    if group not in BLOOD_GROUPS:
        raise HTTPException(status_code=400, detail="unknown blood group")
    return group[:-1], "POS" if group.endswith("+") else "NEG"


def _parse_simulated(raw: str | None) -> datetime:
    """A datetime-local value, read as Harare local time.

    The browser sends '2026-08-16T21:00' with no offset. Treating that as UTC
    would shift every opening-hours decision by two hours and the Sunday
    evening scenario would resolve to a point that is open.
    """
    if not raw:
        return datetime.now(HARARE)
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=HARARE)
    except ValueError:
        return datetime.now(HARARE)


@router.get("/", response_class=HTMLResponse)
def request_form(request: Request, session: Session = Depends(unscoped_session)):
    facilities = session.execute(select(Facility).order_by(Facility.name)).scalars().all()
    return _TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard_form.html",
        context={
            "facilities": [{"id": str(f.id), "name": f.name} for f in facilities],
            "blood_groups": BLOOD_GROUPS,
            "urgencies": URGENCIES,
            "now_local": datetime.now(HARARE).strftime("%Y-%m-%dT%H:%M"),
            "selected_facility": request.cookies.get(FACILITY_COOKIE, ""),
        },
    )


@router.post("/requests")
def create_request(
    facility_id: UUID = Form(...),
    blood_group: str = Form(...),
    units_required: int = Form(4),
    urgency: str = Form(...),
    simulated_time: str | None = Form(None),
):
    """Raise a deficit request. Everything below is one transaction."""
    abo, rh = _split_group(blood_group)
    if urgency not in URGENCIES:
        raise HTTPException(status_code=400, detail="unknown urgency")
    now = _parse_simulated(simulated_time)

    with scoped_session(
        facility_id=facility_id, actor_id="dashboard", role="CLINICIAN"
    ) as session:
        facility = session.get(Facility, facility_id)
        if facility is None:
            raise HTTPException(status_code=404, detail="unknown facility")

        point, reason = resolve_collection_point(session, facility, now)

        request_row = DeficitRequest(
            facility_id=facility_id,
            collection_point_id=point.id if point else None,
            abo=abo,
            rh=rh,
            units_required=units_required,
            urgency=urgency,
            state="MATCHING",
        )
        session.add(request_row)
        session.flush()

        if point is None:
            # FR-COL-04. Recorded and refused, upstream of matching, so no
            # donor is touched and nothing is queued. The facility is told why
            # so it can pursue alternative sourcing (FR-ESC-03).
            request_row.state = "REJECTED_NO_POINT"
            request_row.rejection_reason = reason
            session.flush()
            audit.emit(
                action="REQUEST_REJECTED_NO_COLLECTION_POINT",
                actor=f"facility:{facility_id}",
                target_class="DeficitRequest",
                target_id=request_row.id,
                correlation_id=request_row.id,
                detail={"reason": reason, "urgency": urgency},
            )
            log.info("dashboard.request.rejected", request_id=str(request_row.id))
            request_id = request_row.id
        else:
            result = match_and_reserve(session, point.id, abo, rh, units_required, now)
            request_row.radius_metres = result.radius_metres

            # The simulated clock drives DECISIONS - opening hours, eligibility
            # dates. Dispatch scheduling uses the real clock, because
            # next_attempt_at set to a simulated future time would leave the
            # worker with nothing eligible to drain and the demo would show
            # alerts that never send.
            create_alerts_and_outbox(
                session, request_row, result.reserved_donors, datetime.now(HARARE)
            )

            request_row.state = "ACTIVE"
            request_row.matched_stats = result.stats
            session.flush()
            log.info(
                "dashboard.request.raised",
                request_id=str(request_row.id),
                reserved=result.stats.get("reserved"),
                radius_metres=result.radius_metres,
            )
            request_id = request_row.id

    response = RedirectResponse(url=f"/requests/{request_id}", status_code=303)
    # Carries the scope to the detail page. Not authentication - see deps.py.
    response.set_cookie(FACILITY_COOKIE, str(facility_id), httponly=True, samesite="lax")
    return response


@router.get("/requests/{request_id}", response_class=HTMLResponse)
def request_detail(
    request: Request,
    request_id: UUID,
    session: Session = Depends(get_scoped_session),
):
    summary = readmodel.request_summary(session, request_id)
    if not summary:
        # Either it does not exist, or it belongs to another facility and RLS
        # has hidden it. Deliberately the same answer.
        raise HTTPException(status_code=404, detail="request not found")

    return _TEMPLATES.TemplateResponse(
        request=request,
        name="request_detail.html",
        context={
            "summary": summary,
            "progress": readmodel.alert_progress(session, request_id),
            "wards": readmodel.ward_aggregates(session, request_id),
            "donor_base_url": settings.donor_base_url,
        },
    )
