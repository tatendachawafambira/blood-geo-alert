"""Donor response routes. Two buttons, no account, no JavaScript.

Everything on the page is chosen against one constraint: the reference device
is a feature phone on 2G, and every kilobyte is a proportion of donors who do
not respond (FR-TOK-12, NFR-PERF-04).

What is deliberately NOT here, per FR-TOK-07: the donor's name, their blood
group, their donation history, and any patient or clinical detail. Handsets in
the target population are shared. The residual risk is stated openly in the
SRS - whoever holds the handset within the token's lifetime can answer on the
donor's behalf - and the consequence is a false attendance, not the disclosure
of a health attribute, precisely because there is nothing on this page worth
disclosing.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from bga.config import settings
from bga.domain import audit, tokens

log = structlog.get_logger("bga.donor")

router = APIRouter()

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

if not settings.database_url_donor:
    raise RuntimeError(
        "database_url_donor is not set. The donor application will not fall back "
        "to the application role: bga_app can read the whole donor register, and "
        "ADR-016 exists so that this process cannot. Add to .env:\n"
        "  DATABASE_URL_DONOR=postgresql+psycopg://bga_donor:<password>@127.0.0.1:5432/bga"
    )

# Its own engine and session factory, not the ones in bga.db.
donor_engine = create_engine(settings.database_url_donor, pool_pre_ping=True)
DonorSessionLocal = sessionmaker(bind=donor_engine, expire_on_commit=False)

# ADR-014 ranks. CONFIRMED and DECLINED share rank 4: they are alternative
# answers to the same question, not a progression.
_RANKS = {"OPENED": 3, "CONFIRMED": 4, "DECLINED": 4}

_ADVANCE = text(
    """
    UPDATE core.alert
       SET state = :state, state_rank = :rank,
           opened_at = COALESCE(opened_at, :now),
           responded_at = CASE WHEN :responded THEN COALESCE(responded_at, :now)
                               ELSE responded_at END
     WHERE id = :alert_id AND state_rank < :rank
    RETURNING id
    """
)

# Only the two columns bga_donor is granted on core.deficit_request are named.
# Selecting abo here would fail with "permission denied for table
# deficit_request", which is the boundary working.
_COLLECTION_POINT = text(
    """
    SELECT cp.name, cp.open_hour, cp.close_hour, cp.open_saturday, cp.open_sunday,
           ST_Y(CAST(cp.location AS geometry)) AS latitude,
           ST_X(CAST(cp.location AS geometry)) AS longitude
      FROM core.alert a
      JOIN core.deficit_request dr ON dr.id = a.deficit_request_id
      JOIN core.collection_point cp ON cp.id = dr.collection_point_id
     WHERE a.id = :alert_id
    """
)

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _closed_page(request: Request) -> HTMLResponse:
    """The one page shown for invalid, expired and already-used tokens.

    Byte-identical in all three cases, and carrying no detail about which it
    was. Anything that distinguishes them tells whoever is holding the link
    whether the token was ever real (FR-TOK-11).
    """
    return _TEMPLATES.TemplateResponse(
        request=request, name="donor_closed.html", context={}, status_code=200
    )


def _opening_hours(point) -> str:
    today = _WEEKDAYS[datetime.now(timezone.utc).weekday()]
    weekday = datetime.now(timezone.utc).weekday()
    if weekday == 5 and not point.open_saturday:
        return f"Closed on {today}"
    if weekday == 6 and not point.open_sunday:
        return f"Closed on {today}"
    return f"Open today {point.open_hour:02d}:00 to {point.close_hour:02d}:00"


@router.get("/d/{token}", response_class=HTMLResponse)
def show_alert(request: Request, token: str) -> Response:
    """Render the response page. Does NOT consume the token.

    Opening the link twice before answering must not burn it - people tap,
    lose signal, and tap again.
    """
    with DonorSessionLocal() as session, session.begin():
        alert = tokens.peek(session, token)
        if alert is None:
            return _closed_page(request)

        point = session.execute(_COLLECTION_POINT, {"alert_id": alert.id}).first()

        # Opening the message is itself a delivery signal the dashboard counts.
        session.execute(
            _ADVANCE,
            {
                "alert_id": alert.id,
                "state": "OPENED",
                "rank": _RANKS["OPENED"],
                "now": _now(),
                "responded": False,
            },
        )

        context = {
            "token": token,
            "point_name": point.name if point else "the collection point",
            "hours": _opening_hours(point) if point else "",
        }

    return _TEMPLATES.TemplateResponse(request=request, name="donor.html", context=context)


@router.post("/d/{token}", response_class=HTMLResponse)
def submit_response(request: Request, token: str, response: str = Form(...)) -> Response:
    """Record the donor's answer. One token, one answer, ever."""
    state = "CONFIRMED" if response == "confirm" else "DECLINED"

    with DonorSessionLocal() as session, session.begin():
        alert = tokens.redeem(session, token)
        if alert is None:
            return _closed_page(request)

        session.execute(
            _ADVANCE,
            {
                "alert_id": alert.id,
                "state": state,
                "rank": _RANKS[state],
                "now": _now(),
                "responded": True,
            },
        )

        point = session.execute(_COLLECTION_POINT, {"alert_id": alert.id}).first()
        context = {
            "confirmed": state == "CONFIRMED",
            "point_name": point.name if point else "the collection point",
            "hours": _opening_hours(point) if point else "",
            "latitude": point.latitude if point else None,
            "longitude": point.longitude if point else None,
        }
        alert_id, correlation_id = alert.id, alert.deficit_request_id

    # Outside the transaction and on the audit role's own connection. The
    # detail carries no token and no identifier of the donor.
    audit.emit(
        action=f"DONOR_{state}",
        actor="donor",
        target_class="Alert",
        target_id=alert_id,
        correlation_id=correlation_id,
        detail={"response": state},
    )
    log.info("donor.responded", alert_id=str(alert_id), state=state)

    return _TEMPLATES.TemplateResponse(
        request=request, name="donor_result.html", context=context
    )


@router.get("/d/{token}/qr")
def token_qr(token: str) -> Response:
    """PNG of the full donor URL, for handing a phone to someone in the room.

    Generated from the token as given. An invalid token yields a QR code for a
    page that will show the closed message - which is the same non-answer the
    link itself gives, so this endpoint discloses nothing either.
    """
    import qrcode

    image = qrcode.make(f"{settings.donor_base_url}/d/{token}")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(content=buffer.getvalue(), media_type="image/png")
