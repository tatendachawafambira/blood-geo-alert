"""Delivery receipt ingestion. ADR-014.

Receipts arrive late, duplicated, out of order, and sometimes hours after the
request has closed. The alert state machine is therefore monotonic: a
transition applies only where it strictly advances the rank, so a DELIVERED
receipt that overtakes a CONFIRMED response is recorded as an observation and
changes nothing.

TWO THINGS WORTH KNOWING BEFORE READING THE CODE
------------------------------------------------
1. Resolving the alert. The webhook knows only a provider_message_id. It
   cannot look that up in core.alert, because core.alert has FORCE ROW LEVEL
   SECURITY and the webhook has no facility scope - the query would return
   nothing and the handler would report "already advanced". core.outbox has no
   RLS and the worker writes provider_message_id into its payload on success,
   so that is what resolves the id to an alert and a facility.

2. Only DELIVERED and OPENED may be applied. The rank table puts SUPPRESSED
   and DEAD at 5, above CONFIRMED at 4. A provider reporting "failed" would
   therefore be able to overwrite a donor's confirmation and corrupt the
   conversion rate the pilot exists to measure - and SRS 5.5 is explicit that a
   failed or absent receipt is UNCONFIRMED, not failed. SUPPRESSED and DEAD are
   states this system decides, never states a gateway reports.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import text

from bga.adapters.base import get_adapter
from bga.db import SessionLocal, scoped_session

log = structlog.get_logger("bga.receipts")

router = APIRouter(tags=["receipts"])

# ADR-014, verbatim.
STATE_RANKS = {
    "QUEUED": 0,
    "SENT": 1,
    "DELIVERED": 2,
    "OPENED": 3,
    "CONFIRMED": 4,
    "DECLINED": 4,
    "SUPPRESSED": 5,
    "DEAD": 5,
}

# What a gateway is permitted to tell us. See note 2 above.
RECEIPT_REPORTABLE = frozenset({"DELIVERED", "OPENED"})

_MONOTONIC_TRANSITION = text(
    """
    UPDATE core.alert
       SET state = :new_state, state_rank = :new_rank,
           delivered_at = COALESCE(delivered_at, :now)
     WHERE provider_message_id = :pmid
       AND state_rank < :new_rank
    RETURNING id
    """
)

# core.outbox carries no RLS, so it is the only table that can turn a provider
# message id into a facility scope.
_RESOLVE = text(
    """
    SELECT alert_id, payload ->> 'facility_id' AS facility_id
      FROM core.outbox
     WHERE payload ->> 'provider_message_id' = :pmid
     LIMIT 1
    """
)


@router.post("/webhooks/receipts/{adapter_name}")
async def ingest_receipt(adapter_name: str, request: Request) -> Response:
    """Ingest one delivery receipt. Always 200 unless the signature fails.

    A provider that receives anything other than 2xx retries, often for hours.
    Every outcome below except a failed signature is therefore a 200: an
    already-advanced alert and an unknown message id are both normal, and
    telling the provider to keep trying would achieve nothing.
    """
    try:
        adapter = get_adapter(adapter_name)
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown adapter") from None

    raw_body = await request.body()

    # Verified against the RAW bytes: a signature is computed over exactly what
    # was sent, and re-serialising the JSON would change it.
    if not adapter.verify_webhook(dict(request.headers), raw_body):
        log.warning("receipt.rejected", adapter=adapter_name, reason="signature")
        raise HTTPException(status_code=401, detail="signature verification failed")

    try:
        payload = await request.json()
        provider_message_id, new_state = adapter.parse_receipt(payload)
    except Exception as exc:  # noqa: BLE001 - a malformed body is the provider's bug
        log.warning("receipt.unparseable", adapter=adapter_name, error=str(exc))
        return Response(status_code=200)

    if new_state not in RECEIPT_REPORTABLE:
        # Recorded, not applied. See note 2 in the module docstring.
        log.info(
            "receipt.not_applicable",
            adapter=adapter_name,
            provider_message_id=provider_message_id,
            reported_state=new_state,
            reason="a gateway may not set this state",
        )
        return Response(status_code=200)

    new_rank = STATE_RANKS[new_state]

    with SessionLocal() as lookup:
        resolved = lookup.execute(_RESOLVE, {"pmid": provider_message_id}).first()

    if resolved is None or resolved.facility_id is None:
        log.info(
            "receipt.unknown_message",
            adapter=adapter_name,
            provider_message_id=provider_message_id,
        )
        return Response(status_code=200)

    with scoped_session(
        facility_id=resolved.facility_id, actor_id=f"gateway:{adapter_name}", role="GATEWAY"
    ) as session:
        advanced = session.execute(
            _MONOTONIC_TRANSITION,
            {
                "pmid": provider_message_id,
                "new_state": new_state,
                "new_rank": new_rank,
                "now": datetime.now(timezone.utc),
            },
        ).first()

    if advanced is None:
        # Not an error. The alert is already at or past this state - a
        # duplicate receipt, or one that arrived after the donor responded.
        log.debug(
            "receipt.already_advanced",
            adapter=adapter_name,
            provider_message_id=provider_message_id,
            reported_state=new_state,
        )
    else:
        log.info(
            "receipt.applied",
            adapter=adapter_name,
            alert_id=str(advanced.id),
            state=new_state,
        )

    return Response(status_code=200)
