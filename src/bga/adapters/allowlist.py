"""Who a demonstration gateway is permitted to actually message.

WHY THIS EXISTS
---------------
The seeded register holds 800 donors whose numbers were generated with real
Zimbabwean network prefixes (71, 73, 77, 78) and random subscriber digits.
They are structurally valid live numbers, and a meaningful proportion of them
belong to real people who never consented to anything.

A single request for four units matches up to forty donors (FR-GEO-07: ten per
unit). Pointing a real gateway at that register without a further control
would send unsolicited SMS to strangers - which is a harm in itself, and for a
platform whose entire design rests on consent under the Cyber and Data
Protection Act, an unusually bad one.

The dispatch cap does not help here: it bounds how many donors are alerted, not
which. Eligibility does not help: a seeded donor is perfectly eligible. The
control has to sit at the transport boundary, because that is the last point
where a real message can still be stopped.

FAIL-CLOSED, DELIBERATELY
-------------------------
An empty allowlist blocks everything rather than allowing everything. The
alternative - empty means unrestricted - would mean a forgotten setting sends
forty real messages, and the failure would be silent and irreversible. A
blocked demonstration costs a minute; an unsolicited SMS to a stranger cannot
be recalled.

This applies ONLY to the demonstration adapters (twilio, android). The
production Africa's Talking path has no allowlist, because by then the register
holds consented donors rather than synthetic ones.
"""

from __future__ import annotations

import structlog

from bga.config import settings

log = structlog.get_logger("bga.gateway.allowlist")


def allowed_numbers() -> set[str]:
    """The permitted recipients, normalised to E.164."""
    raw = settings.sms_allowlist or ""
    out: set[str] = set()
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            from bga.crypto.field import normalise_msisdn

            out.add(normalise_msisdn(piece))
        except Exception:
            # A malformed entry must not silently widen the allowlist, and must
            # not crash dispatch either. Drop it and say so.
            log.warning("gateway.allowlist.unparsable_entry")
    return out


def blocked_reason(to_e164: str) -> str | None:
    """None if this number may be messaged, else why it may not be."""
    permitted = allowed_numbers()

    if not permitted:
        return (
            "SMS_ALLOWLIST is empty, so no real message may be sent. This is "
            "the safe default: the seeded register contains structurally valid "
            "Zimbabwean numbers belonging to real people. Add your own test "
            "number(s) to SMS_ALLOWLIST in .env, comma separated."
        )

    if to_e164 not in permitted:
        # The number itself is never logged or returned - it belongs to someone.
        return "recipient is not on SMS_ALLOWLIST; suppressed before submission"

    return None
