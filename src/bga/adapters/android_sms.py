"""Android-handset SMS adapter. The phone in your pocket is the gateway.

WHY THIS EXISTS
---------------
Every hosted gateway needs either money or a registration process measured in
weeks. A demonstration needs neither: an Android phone already holds a SIM that
can send an SMS today, on the network the recipients actually use.

An SMS gateway application on the handset exposes a small HTTP API on the local
network. The dispatch worker POSTs to it exactly as it would to a cloud
provider, and the handset sends the message over GSM from its own SIM.

Verified against android-sms-gateway (github.com/capcom6/android-sms-gateway)
running in LOCAL SERVER mode, which is free and open source. Its local API is:

    POST http://<phone-ip>:8080/message
    Authorization: Basic <username:password from the app>
    {"message": "...", "phoneNumbers": ["+263771234567"]}

    -> {"id": "...", "state": "Pending", ...}

WHAT THIS IS AND IS NOT
-----------------------
It is a real SMS. It leaves a real SIM, crosses the mobile network, and arrives
on a handset that has no connection to this laptop. Everything upstream of the
adapter - matching, eligibility, the frequency cap, the outbox, retry, the
dead letter queue - is the production path, unchanged.

It is not a production gateway. Throughput is one message every few seconds,
the phone must stay awake and on the same network as the worker, and the
messages come from a personal number rather than a registered sender ID. It
does not replace Africa's Talking; it demonstrates the seam that Africa's
Talking will eventually plug into.

The cost is ordinary person-to-person SMS tariff on the SIM, which is why this
is the option that needs no funding.
"""

from __future__ import annotations

import hmac
import threading

import httpx
import structlog

from bga.adapters.allowlist import blocked_reason
from bga.adapters.base import SubmitResult
from bga.config import settings

log = structlog.get_logger("bga.gateway.android")

# Handset app states -> our alert states.
_RECEIPT_STATES = {
    "pending": "QUEUED",
    "processed": "SENT",
    "sent": "SENT",
    "delivered": "DELIVERED",
    "failed": "FAILED",
}


class AndroidSmsAdapter:
    """Submits through an SMS gateway app running on an Android handset."""

    name = "android"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Same idempotency position as the Twilio adapter: in-process cover for
        # a retry after an ambiguous timeout, with the durable guarantee living
        # in the outbox and the unique constraint on (deficit_request_id,
        # donor_id).
        self._issued: dict[str, str] = {}

    def _endpoint(self) -> tuple[str, tuple[str, str] | None]:
        base = (settings.android_sms_url or "").rstrip("/")
        if not base:
            raise RuntimeError(
                "The android gateway is selected but not configured. Add to .env:\n"
                "  ANDROID_SMS_URL=http://<phone-ip>:8080\n"
                "  ANDROID_SMS_USERNAME=sms\n"
                "  ANDROID_SMS_PASSWORD=<password shown in the app>\n"
                "The phone must be on the same network as this worker."
            )
        user = settings.android_sms_username
        password = settings.android_sms_password
        auth = (user, password) if user and password else None
        return f"{base}/message", auth

    def submit(self, to_e164: str, body: str, idempotency_key: str) -> SubmitResult:
        # Checked BEFORE the idempotency cache and before any network call: a
        # number that must not be messaged must not be messaged on a retry
        # either. Not retriable - the allowlist will not change mid-run, so
        # retrying is pure noise and the dead letter queue is the right home.
        blocked = blocked_reason(to_e164)
        if blocked is not None:
            log.warning("gateway.android.suppressed_by_allowlist")
            return SubmitResult(
                accepted=False,
                provider_message_id=None,
                error=blocked,
                retriable=False,
            )

        with self._lock:
            existing = self._issued.get(idempotency_key)
        if existing is not None:
            return SubmitResult(
                accepted=True, provider_message_id=existing, error=None, retriable=False
            )

        url, auth = self._endpoint()
        try:
            response = httpx.post(
                url,
                json={"message": body, "phoneNumbers": [to_e164]},
                auth=auth,
                timeout=25.0,
            )
        except httpx.HTTPError as exc:
            # The commonest cause here is the phone dropping off the network or
            # the app being backgrounded. Retriable: nothing was accepted.
            return SubmitResult(
                accepted=False,
                provider_message_id=None,
                error=f"handset unreachable: {type(exc).__name__}",
                retriable=True,
            )

        if response.status_code in (200, 201, 202):
            try:
                message_id = str(response.json().get("id") or "")
            except Exception:
                message_id = ""
            if not message_id:
                message_id = f"android-{idempotency_key[:16]}"
            with self._lock:
                message_id = self._issued.setdefault(idempotency_key, message_id)
            return SubmitResult(
                accepted=True,
                provider_message_id=message_id,
                error=None,
                retriable=False,
            )

        permanent = 400 <= response.status_code < 500 and response.status_code not in (
            408,
            429,
        )
        detail = response.text[:180] if response.text else f"HTTP {response.status_code}"
        log.warning(
            "gateway.android.rejected",
            status=response.status_code,
            retriable=not permanent,
        )
        return SubmitResult(
            accepted=False,
            provider_message_id=None,
            error=f"handset gateway {response.status_code}: {detail}",
            retriable=not permanent,
        )

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        """Compare a shared secret in constant time.

        The handset app signs nothing, so this is the weakest verification in
        the system and it is deliberately fail-closed: with no secret set, no
        callback is accepted. That is the correct default - the mock's
        unconditional True is acceptable only because the mock never leaves a
        developer machine.
        """
        secret = settings.android_sms_webhook_secret
        if not secret:
            return False
        supplied = headers.get("x-webhook-secret") or headers.get("X-Webhook-Secret")
        if not supplied:
            return False
        return hmac.compare_digest(secret, supplied)

    def parse_receipt(self, payload: dict) -> tuple[str, str]:
        provider_message_id = payload.get("id") or payload.get("provider_message_id")
        raw_status = str(payload.get("state") or payload.get("status") or "").strip().lower()

        if not provider_message_id:
            raise ValueError("receipt has no provider message id")
        if raw_status not in _RECEIPT_STATES:
            raise ValueError(f"unrecognised receipt status {raw_status!r}")

        return str(provider_message_id), _RECEIPT_STATES[raw_status]
