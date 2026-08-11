"""Internal JSON endpoints.

Block 9 adds /internal/health and /internal/metrics here. For now this holds
the polling endpoint the request detail page uses to update its counters
without a reload.

Everything returned is an aggregate. This is the same read model the page
itself renders, so the JSON surface cannot expose an individual either - there
is no separate query path to get wrong (ADR-005).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from bga.api.deps import get_scoped_session
from bga.domain import readmodel

router = APIRouter(tags=["internal"])


@router.get("/api/requests/{request_id}/status")
def request_status(
    request_id: UUID, session: Session = Depends(get_scoped_session)
) -> dict:
    """Counters for one request. Polled every two seconds by the detail page."""
    summary = readmodel.request_summary(session, request_id)
    if not summary:
        # Absent, or belonging to another facility and hidden by RLS. The same
        # answer either way.
        raise HTTPException(status_code=404, detail="request not found")

    progress = readmodel.alert_progress(session, request_id)

    return {
        "request_state": summary["state"],
        "counts": progress["counts"],
        "total": progress["total"],
        "units_required": progress["units_required"],
        "confirmed": progress["confirmed"],
        "pledged_label": progress["pledged_label"],
        "ward_aggregates": readmodel.ward_aggregates(session, request_id),
    }
