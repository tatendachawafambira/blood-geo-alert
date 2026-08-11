"""Operator view of the dispatch queue. The simulated gateway, made visible.

This is the page that shows a panel what the outbox actually is: rows with
states, attempt counts and backoff times, draining through an adapter. It is
also where the failure rate is turned up to demonstrate retry, failover and the
dead letter queue live rather than describing them.

The mobile number is masked here and nowhere is it rendered in full. The
console decrypts in memory to produce '+263 77* *** 567' and discards the
plaintext on the next line - enough for an operator to correlate a message with
a delivery report, not enough to dial.
"""

from __future__ import annotations

import base64
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from bga.adapters import mock_sms
from bga.adapters.base import primary_gateway_name, secondary_gateway_name
from bga.api.deps import get_scoped_session
from bga.crypto.field import decrypt_msisdn, mask_msisdn

log = structlog.get_logger("bga.gateway")

router = APIRouter(tags=["gateway"])

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

FAILURE_RATES = (0.0, 0.3, 1.0)

# Joined through alert, so row-level security scopes the console to the
# selected facility exactly as it scopes the dashboard.
_QUEUE = text(
    """
    SELECT o.id            AS outbox_id,
           o.state         AS outbox_state,
           o.attempts,
           o.next_attempt_at,
           o.last_error,
           o.payload,
           a.id            AS alert_id,
           a.state         AS alert_state,
           a.provider_message_id,
           a.queued_at,
           a.submitted_at,
           d.preferred_language
      FROM core.outbox o
      JOIN core.alert a ON a.id = o.alert_id
      JOIN core.donor d ON d.id = a.donor_id
     ORDER BY a.queued_at DESC, o.next_attempt_at DESC
     LIMIT 100
    """
)

_DEAD_LETTERS = text("SELECT count(*) FROM core.dead_letter")


def _adapter_for(provider_message_id: str | None) -> str:
    """Which adapter handled this row.

    Derived from the message id rather than read from a column: nothing
    persists the adapter used per message. Worth adding before a second real
    provider exists, because after failover the operator cannot otherwise tell
    which gateway carried which message.
    """
    if provider_message_id is None:
        return "-"
    if provider_message_id.startswith("mock-"):
        return "mock"
    return primary_gateway_name()


@router.get("/gateway", response_class=HTMLResponse)
def gateway_console(request: Request, session: Session = Depends(get_scoped_session)):
    rows = []
    for row in session.execute(_QUEUE):
        payload = row.payload or {}

        # In memory, for one line, then gone.
        masked = "-"
        ciphertext_b64 = payload.get("msisdn_ciphertext_b64")
        if ciphertext_b64:
            try:
                masked = mask_msisdn(decrypt_msisdn(base64.b64decode(ciphertext_b64)))
            except Exception:  # noqa: BLE001 - a bad row must not blank the page
                masked = "(undecryptable)"

        rows.append(
            {
                "queued_at": row.queued_at,
                "masked_msisdn": masked,
                "language": row.preferred_language,
                "body": payload.get("body", ""),
                "outbox_state": row.outbox_state,
                "alert_state": row.alert_state,
                "attempts": row.attempts,
                "next_attempt_at": row.next_attempt_at,
                "last_error": row.last_error,
                "adapter": _adapter_for(row.provider_message_id),
                "provider_message_id": row.provider_message_id,
            }
        )

    return _TEMPLATES.TemplateResponse(
        request=request,
        name="gateway_console.html",
        context={
            "rows": rows,
            "dead_letters": session.execute(_DEAD_LETTERS).scalar() or 0,
            "failure_rate": mock_sms.current_failure_rate(),
            "failure_rates": FAILURE_RATES,
            "primary": primary_gateway_name(),
            "secondary": secondary_gateway_name(),
        },
    )


@router.post("/gateway/failure-rate")
def set_failure_rate(rate: float = Form(...)):
    """Set the simulated failure rate for every process on this machine.

    Written to a file rather than a module global because the worker that
    actually submits is a different process - see adapters/mock_sms.py.
    """
    applied = mock_sms.set_failure_rate(rate)
    log.warning("gateway.failure_rate.set", rate=applied)
    return RedirectResponse(url="/gateway", status_code=303)
