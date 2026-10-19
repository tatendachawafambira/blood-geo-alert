"""Twilio SMS adapter. Real messages, over the seam base.py defines.

WHY THIS EXISTS ALONGSIDE africastalking.py
-------------------------------------------
Africa's Talking is the intended production gateway, and its blocker is not
code: an alphanumeric sender ID must be registered and approved by Econet,
NetOne and Telecel separately, which is weeks of paperwork (OI-04). Twilio's
trial account issues a sending number immediately and carries free credit, so
it is the only route to a genuinely delivered SMS inside a day.

This is a demonstration gateway, not the production one. Two limits are
inherent to a trial account and must be understood before it is trusted:

  Trial accounts send only to VERIFIED numbers. Every recipient must be added
  under Verified Caller IDs in the Twilio console first. An unverified number
  is rejected with error 21608 - which this adapter classifies as permanent,
  not retriable, so it goes to the dead letter queue instead of being retried
  forever.

  Trial messages carry a prefix. Twilio prepends "Sent from your Twilio trial
  account - " (about 38 characters) to every body. Our templates are built to
  land inside one 160-septet GSM-7 segment (FR-MSG-02); the prefix pushes them
  into two. That costs trial credit and nothing else, but it means a segment
  count measured against a trial account is not the real one.

Delivery receipts require a publicly reachable HTTPS callback. Without one -
and a laptop on a hotspot does not have one - messages reach the handset and
settle at SENT rather than DELIVERED. That is FR-MSG-06 behaving correctly:
absence of a receipt is reported as unconfirmed, never as failed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading

import httpx
import structlog

from bga.adapters.allowlist import blocked_reason
from bga.adapters.base import SubmitResult
from bga.config import settings

log = structlog.get_logger("bga.gateway.twilio")

API_ROOT = "https://api.twilio.com/2010-04-01"

# Twilio status values -> our alert states. Anything absent from this map is
# rejected by parse_receipt rather than guessed at.
_RECEIPT_STATES = {
    "queued": "QUEUED",
    "sending": "SENT",
    "sent": "SENT",
    "delivered": "DELIVERED",
    "undelivered": "FAILED",
    "failed": "FAILED",
}

# Twilio error codes that will never succeed on retry, whatever we do. Retrying
# these burns credit and delays the dead letter queue that should already hold
# them. 21608 is the one a trial account hits constantly: unverified recipient.
_PERMANENT_CODES = {
    21211,  # invalid 'To' number
    21214,  # 'To' number is not a valid mobile number
    21408,  # permission to send to this region not enabled
    21608,  # trial account: unverified recipient
    21610,  # recipient has unsubscribed
    21614,  # 'To' number is not SMS-capable
}


class TwilioAdapter:
    """Submits over Twilio's REST API. One HTTP call, no SDK, no new dependency."""

    name = "twilio"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Twilio's Messages API has no idempotency key of its own, so the same
        # problem africastalking.py records applies here. This map covers the
        # retry-after-ambiguous-timeout case within one worker lifetime; the
        # durable guarantee is the outbox row plus the unique constraint on
        # (deficit_request_id, donor_id), which survives a restart and this
        # map does not.
        self._issued: dict[str, str] = {}

    # --- configuration -----------------------------------------------------

    def _credentials(self) -> tuple[str, str, str]:
        sid = settings.twilio_account_sid
        token = settings.twilio_auth_token
        sender = settings.twilio_from_number
        if not (sid and token and sender):
            raise RuntimeError(
                "Twilio is selected as a gateway but is not configured. Add to .env:\n"
                "  TWILIO_ACCOUNT_SID=AC...\n"
                "  TWILIO_AUTH_TOKEN=...\n"
                "  TWILIO_FROM_NUMBER=+1...\n"
                "The token is read through settings and is never logged (NFR-SEC-04)."
            )
        return sid, token, sender

    # --- the seam ----------------------------------------------------------

    def submit(self, to_e164: str, body: str, idempotency_key: str) -> SubmitResult:
        # Before the idempotency cache and before the network call. A trial
        # account's verified-caller list is a second, independent guard, but it
        # is Twilio's and can be changed in their console - this one is ours.
        blocked = blocked_reason(to_e164)
        if blocked is not None:
            log.warning("gateway.twilio.suppressed_by_allowlist")
            return SubmitResult(
                accepted=False,
                provider_message_id=None,
                error=blocked,
                retriable=False,
            )

        with self._lock:
            existing = self._issued.get(idempotency_key)
        if existing is not None:
            # Same ordering as the mock, for the same reason: an already
            # accepted key keeps its id even if the provider is now failing.
            return SubmitResult(
                accepted=True, provider_message_id=existing, error=None, retriable=False
            )

        sid, token, sender = self._credentials()
        form = {"To": to_e164, "From": sender, "Body": body}
        if settings.twilio_status_callback:
            form["StatusCallback"] = settings.twilio_status_callback

        try:
            response = httpx.post(
                f"{API_ROOT}/Accounts/{sid}/Messages.json",
                data=form,
                auth=(sid, token),
                timeout=20.0,
            )
        except httpx.HTTPError as exc:
            # Transport failure. Nothing was accepted, so a retry is correct
            # and cannot duplicate. The exception text carries no MSISDN.
            return SubmitResult(
                accepted=False,
                provider_message_id=None,
                error=f"transport error: {type(exc).__name__}",
                retriable=True,
            )

        if response.status_code in (200, 201):
            message_sid = response.json().get("sid")
            if not message_sid:
                return SubmitResult(
                    accepted=False,
                    provider_message_id=None,
                    error="accepted without a message sid",
                    retriable=True,
                )
            with self._lock:
                message_sid = self._issued.setdefault(idempotency_key, message_sid)
            return SubmitResult(
                accepted=True,
                provider_message_id=message_sid,
                error=None,
                retriable=False,
            )

        # Failure. Classify it, because retriable is the difference between a
        # transient blip and forty messages hammering a permanent rejection.
        try:
            detail = response.json()
            code = int(detail.get("code") or 0)
            message = str(detail.get("message") or "")[:180]
        except Exception:
            code, message = 0, f"HTTP {response.status_code}"

        permanent = code in _PERMANENT_CODES or (
            400 <= response.status_code < 500 and response.status_code != 429
        )
        if code == 21608:
            message = (
                "recipient not verified on this Twilio trial account - add it "
                "under Verified Caller IDs"
            )

        log.warning(
            "gateway.twilio.rejected",
            status=response.status_code,
            code=code,
            retriable=not permanent,
        )
        return SubmitResult(
            accepted=False,
            provider_message_id=None,
            error=f"twilio {code}: {message}" if code else message,
            retriable=not permanent,
        )

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        """Validate X-Twilio-Signature over the callback URL and its parameters.

        Twilio signs base64(HMAC-SHA1(auth_token, url + sorted key/value pairs))
        for form-encoded posts. Unlike the mock this can actually fail, and it
        must: without it anyone who guesses a message sid can mark an alert
        delivered.
        """
        _sid, token, _sender = self._credentials()
        supplied = headers.get("x-twilio-signature") or headers.get("X-Twilio-Signature")
        url = settings.twilio_status_callback
        if not supplied or not url:
            return False

        from urllib.parse import parse_qsl

        pairs = sorted(parse_qsl(raw_body.decode("utf-8", "replace"), keep_blank_values=True))
        signed = url + "".join(k + v for k, v in pairs)
        expected = base64.b64encode(
            hmac.new(token.encode(), signed.encode("utf-8"), hashlib.sha1).digest()
        ).decode()
        return hmac.compare_digest(expected, supplied)

    def parse_receipt(self, payload: dict) -> tuple[str, str]:
        provider_message_id = payload.get("MessageSid") or payload.get("SmsSid")
        raw_status = str(
            payload.get("MessageStatus") or payload.get("SmsStatus") or ""
        ).strip().lower()

        if not provider_message_id:
            raise ValueError("receipt has no provider message id")
        if raw_status not in _RECEIPT_STATES:
            raise ValueError(f"unrecognised receipt status {raw_status!r}")

        return str(provider_message_id), _RECEIPT_STATES[raw_status]
