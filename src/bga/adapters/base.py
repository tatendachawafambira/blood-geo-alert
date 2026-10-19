"""The gateway adapter seam.

QAS-09 says a new engineer adding an alert channel changes the adapter layer
and its configuration, and nothing in matching, eligibility or consent. This
Protocol is what that promise rests on: three methods, no shared state, no
knowledge of the outbox or the donor register.

Deliberately a Protocol rather than an abstract base class. An adapter is a
structural contract, not a family with shared behaviour - there is nothing
useful to inherit, and a base class would invite putting something there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from bga.config import settings


@dataclass(frozen=True)
class SubmitResult:
    """What a gateway said about one submission.

    No field has a default, deliberately. `retriable` defaulting to False would
    mean a forgotten argument on a failure path silently sends a message to the
    dead letter queue that should have been retried, and nothing would look
    wrong. Every construction site states all four.
    """

    accepted: bool
    provider_message_id: str | None
    error: str | None
    retriable: bool


@runtime_checkable
class GatewayAdapter(Protocol):
    name: str

    def submit(self, to_e164: str, body: str, idempotency_key: str) -> SubmitResult:
        """Hand one message to the provider.

        The idempotency key is derived from the alert id (ADR-010), so a retry
        after an ambiguous timeout cannot double-send: the provider - or, for
        the mock, the adapter itself - returns the original message id rather
        than accepting a second message.
        """
        ...

    def verify_webhook(self, headers: dict, raw_body: bytes) -> bool:
        """Is this inbound callback genuinely from the provider?

        Takes the RAW body, not a parsed dict: signatures are computed over
        exact bytes, and re-serialising JSON changes them.
        """
        ...

    def parse_receipt(self, payload: dict) -> tuple[str, str]:
        """Return (provider_message_id, alert state) from a delivery receipt."""
        ...


# --- registry --------------------------------------------------------------

# Adapters are cached, not rebuilt per call. The mock keeps its idempotency
# table in the instance, so handing out a fresh object each time would reset it
# and quietly destroy the property ADR-010 depends on.
_INSTANCES: dict[str, GatewayAdapter] = {}


def primary_gateway_name() -> str:
    return settings.primary_gateway


def secondary_gateway_name() -> str:
    return settings.secondary_gateway


def get_adapter(name: str) -> GatewayAdapter:
    """Resolve an adapter by name. Unknown names fail loudly, never silently.

    Falling back to the mock on an unrecognised name would mean a production
    misconfiguration presents as messages that appear to send and never arrive.
    """
    if name in _INSTANCES:
        return _INSTANCES[name]

    # Imported here rather than at module scope so the two adapter modules can
    # import SubmitResult from this one without a circular import.
    from bga.adapters.africastalking import AfricasTalkingAdapter
    from bga.adapters.android_sms import AndroidSmsAdapter
    from bga.adapters.mock_sms import MockSmsAdapter
    from bga.adapters.twilio_sms import TwilioAdapter

    builders = {
        "mock": MockSmsAdapter,
        "africastalking": AfricasTalkingAdapter,
        # Demonstration gateways. Both send a genuinely delivered SMS; neither
        # is the production route. See each module's docstring for what it
        # gives up relative to africastalking.
        "twilio": TwilioAdapter,
        "android": AndroidSmsAdapter,
    }
    if name not in builders:
        raise ValueError(
            f"unknown gateway {name!r}; configured adapters are {sorted(builders)}"
        )

    _INSTANCES[name] = builders[name]()
    return _INSTANCES[name]
