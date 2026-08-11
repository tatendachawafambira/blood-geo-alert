"""Africa's Talking SMS adapter - SKELETON ONLY. Nothing here is implemented.

This file exists to prove the seam, not to use it. Every method raises. There
is no network code and no new dependency, because writing an integration
against credentials we do not hold would produce something that looks finished
and has never once been run.

WHAT IMPLEMENTING THIS ACTUALLY REQUIRES
----------------------------------------
None of it is code, which is the point of recording it here.

  Credentials. An Africa's Talking account, a username, and an API key issued
  per environment. The sandbox username is literally "sandbox" and reaches only
  test numbers. Keys are read through settings like every other secret and are
  never written to disk or a log (NFR-SEC-04).

  A registered sender ID. Alphanumeric sender IDs must be registered with
  Africa's Talking AND approved by each network - Econet, NetOne and Telecel
  separately. This is a commercial and paperwork process measured in weeks, not
  an API call, and it is the long pole in going live. Until it completes,
  messages arrive from a shared short code.

  Delivery report callbacks. A publicly reachable HTTPS endpoint registered in
  the Africa's Talking dashboard. The provider posts form-encoded fields, not
  JSON, which is why parse_receipt takes a dict the router has already decoded
  and verify_webhook takes the raw body.

  Signature verification. Africa's Talking does not sign delivery reports. The
  webhook must therefore be protected by IP allow-listing against their
  published ranges plus a shared secret in the callback URL, and FR-INT-05's
  timestamp and replay checks still apply. verify_webhook returning True
  unconditionally, as the mock does, is not acceptable here: without it anyone
  who guesses a provider message id can mark any alert delivered.

  A commercial quotation. OI-04 in the SRS blocks publishing any cost figure
  until a written per-message price exists for all three networks.

Adding this adapter changes this file and configuration. It does not change
matching, eligibility, consent or the outbox - which is the modifiability
claim in QAS-09, tested for the first time when this is filled in.
"""

from __future__ import annotations

from bga.adapters.base import SubmitResult

# Live and sandbox endpoints, recorded so the seam is concrete.
API_BASE_URL = "https://api.africastalking.com/version1"
SANDBOX_BASE_URL = "https://api.sandbox.africastalking.com/version1"
SEND_MESSAGE_URL = f"{API_BASE_URL}/messaging"

_NOT_IMPLEMENTED = (
    "AfricasTalkingAdapter is a documented seam, not an implementation. "
    "Release 1.0 dispatches through the mock adapter."
)


class AfricasTalkingAdapter:
    name = "africastalking"

    def submit(self, to_e164: str, body: str, idempotency_key: str) -> SubmitResult:
        # Africa's Talking has no idempotency key of its own. A real
        # implementation must therefore keep the mapping itself - most likely
        # by refusing to submit when core.alert already holds a
        # provider_message_id for this alert - or accept duplicate sends on
        # retry, which FR-MSG-06 does not allow.
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def parse_receipt(self, payload: dict) -> tuple[str, str]:
        raise NotImplementedError(_NOT_IMPLEMENTED)
