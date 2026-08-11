"""Message composition and the transactional outbox write. ADR-010, ADR-018.

Nothing here talks to a gateway. Alert rows and outbox rows are written in the
caller's single transaction, and a separate worker drains the queue later. That
is what removes the choice between losing messages - submit after commit, then
the process dies - and sending phantom ones - submit before commit, then the
transaction rolls back.

The clinician's answer therefore does not wait on a provider (ADR-018). What
they are told is how many donors were RESERVED, which is known at commit;
whether anything was delivered is not, and the interface must not imply it is.

On message content: FR-MSG-03 forbids blood group, donor name, patient detail
and clinical information in the body. The reason is not squeamishness. Handsets
in the target population are shared, and a message reading "you are O negative"
discloses a health attribute to whoever picks the phone up. Everything specific
lives behind the tokenised link, which needs the handset AND the token.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import structlog

from bga.config import settings
from bga.domain import audit
from bga.models.reference import CollectionPoint
from bga.models.requests import Alert, Outbox

log = structlog.get_logger(__name__)

_HARARE = ZoneInfo("Africa/Harare")

# Templates take {url} and {point_name} and nothing else. There is no
# substitution variable for a blood group or a name, so FR-MSG-03 cannot be
# violated by editing a template - only by adding a placeholder that does not
# exist, which fails loudly at render time.
#
# NBSZ is named because an unattributed link asking someone to travel is
# indistinguishable from a phishing message. Tone is deliberately flat
# (NFR-USE-06): a system that can send an emotive message to thousands of
# people in seconds can manufacture pressure at scale.
MESSAGE_TEMPLATES = {
    "en": "NBSZ: blood is needed near you. Donate at {point_name}: {url}",
    "sn": "NBSZ: ropa rinodiwa pedyo nemi. Endai ku{point_name}: {url}",
    "nd": "NBSZ: igazi liyadingeka eduze lawe. Hamba e{point_name}: {url}",
}

# GSM 03.38 default alphabet. Anything outside this forces the whole message to
# UCS-2, which halves the segment to 70 characters and would split every
# message we send into two - doubling the cost per mobilisation.
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# These exist in GSM-7 but cost two septets each, via an escape.
_GSM7_EXTENDED = "^{}\\[~]|€"

SEGMENT_SEPTETS = 160

# Worst case inputs for the build-time check. Both are real: the token length
# is what secrets.token_urlsafe(32) produces, and the point name is the longest
# in the seed data.
_TOKEN_LENGTH = 43
_LONGEST_POINT_NAME = "NBSZ Mobile Drive - Glen View Community Hall"


def septet_length(body: str) -> int:
    """Length in GSM-7 septets, counting escaped characters as two."""
    return sum(2 if ch in _GSM7_EXTENDED else 1 for ch in body)


def _non_gsm7_characters(body: str) -> set[str]:
    return {ch for ch in body if ch not in _GSM7_BASIC and ch not in _GSM7_EXTENDED}


def validate_templates() -> None:
    """Raise if any template could produce a message over one segment.

    Called from a test, so a template edit that would silently double the cost
    of every mobilisation fails the build rather than the demo.

    The budget is tighter than it looks. A 43-character token plus a 44
    character collection point name plus the base URL consumes most of the 160
    septets before a single word of the message is written.
    """
    worst_url = f"{settings.donor_base_url}/d/{'T' * _TOKEN_LENGTH}"

    for language, template in MESSAGE_TEMPLATES.items():
        body = template.format(url=worst_url, point_name=_LONGEST_POINT_NAME)

        illegal = _non_gsm7_characters(body)
        if illegal:
            raise ValueError(
                f"{language} template contains characters outside GSM-7 "
                f"({sorted(illegal)}); the message would be sent as UCS-2 and "
                f"split across two segments"
            )

        length = septet_length(body)
        if length > SEGMENT_SEPTETS:
            raise ValueError(
                f"{language} template renders to {length} septets in the worst "
                f"case, over the {SEGMENT_SEPTETS} limit for one segment"
            )


def _local(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=_HARARE)
    return now.astimezone(_HARARE)


def quiet_hours_active(now: datetime) -> bool:
    """True inside the configured quiet window, in Harare local time.

    The window normally wraps midnight (21:00 to 07:00), so it is not a simple
    range test. Evaluated locally because 21:00 in Harare is 19:00 UTC, and a
    UTC hour would open the window two hours early.
    """
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start == end:
        return False

    hour = _local(now).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _end_of_quiet_hours(now: datetime) -> datetime:
    """The next moment quiet hours lift."""
    local = _local(now)
    end = local.replace(hour=settings.quiet_hours_end, minute=0, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end


def _render(language: str, point_name: str, url: str) -> tuple[str, str]:
    """Render the body, falling back to English if a language has no template.

    Falling back rather than raising is deliberate: a donor reaching the
    collection point after an English message is a worse outcome than a donor
    who was never contacted at all, and the fallback is logged at error level
    so it does not stay unnoticed. The database CHECK constraint means this
    should be unreachable.
    """
    if language not in MESSAGE_TEMPLATES:
        log.error("dispatch.template.missing", language=language, fallback="en")
        language = "en"
    return language, MESSAGE_TEMPLATES[language].format(point_name=point_name, url=url)


def create_alerts_and_outbox(
    session, deficit_request, reserved_donors, now: datetime
) -> list[Alert]:
    """Write alerts and their outbox rows in the caller's single transaction.

    No gateway is called here (ADR-018). Nothing is committed here either - the
    caller owns the transaction, and that is the point: matching, reservation,
    alert creation and the outbox write either all land or none do.
    """
    # Deferred import: tokens.py arrives in Block 6, and this module must
    # remain importable before then so validate_templates can be tested.
    # Promote to a module-level import once it exists.
    from bga.domain import tokens

    # Looked up rather than reached through a relationship: DeficitRequest
    # carries collection_point_id but declares no relationship() to it, and
    # models/requests.py is not this block's file to change.
    point = (
        session.get(CollectionPoint, deficit_request.collection_point_id)
        if deficit_request.collection_point_id is not None
        else None
    )
    point_name = point.name if point is not None else "the collection point"

    # FR-MSG-10: a routine request raised at 22:00 waits until the window
    # lifts. URGENT and CRITICAL are exempt - an emergency is precisely when
    # waking someone is justified.
    deferred = quiet_hours_active(now) and deficit_request.urgency == "ROUTINE"
    next_attempt_at = _end_of_quiet_hours(now) if deferred else now

    alerts: list[Alert] = []
    languages: dict[str, int] = {}

    for donor in reserved_donors:
        alert = Alert(
            deficit_request_id=deficit_request.id,
            donor_id=donor.donor_id,
            channel="SMS",
            state="QUEUED",
            state_rank=0,
            queued_at=now,
        )
        session.add(alert)
        # Flush to get alert.id: the token binds to it and the idempotency key
        # is derived from it.
        session.flush()

        raw_token = tokens.issue_token(session, alert, settings.token_ttl_minutes)
        url = f"{settings.donor_base_url}/d/{raw_token}"

        language, body = _render(donor.preferred_language, point_name, url)
        languages[language] = languages.get(language, 0) + 1
        alert.message_body = body

        session.add(
            Outbox(
                alert_id=alert.id,
                payload={
                    "alert_id": str(alert.id),
                    # The ciphertext travels, never the number. The worker
                    # decrypts in memory at submission time and discards it.
                    "msisdn_ciphertext_b64": base64.b64encode(
                        donor.msisdn_ciphertext
                    ).decode("ascii"),
                    "body": body,
                    "idempotency_key": str(alert.id),
                    # Not in the original payload spec, and load-bearing:
                    # core.alert has FORCE ROW LEVEL SECURITY, so a worker
                    # holding no facility scope sees zero alerts and updates
                    # zero rows - silently, with no error. core.outbox has no
                    # RLS, so this is how the scope reaches the worker.
                    "facility_id": str(deficit_request.facility_id),
                },
                attempts=0,
                next_attempt_at=next_attempt_at,
                state="QUEUED",
            )
        )
        alerts.append(alert)

    session.flush()

    # One event for the batch, not one per donor: forty rows saying the same
    # thing bury the one that matters.
    audit.emit(
        action="ALERTS_QUEUED",
        actor=f"facility:{deficit_request.facility_id}",
        target_class="DeficitRequest",
        target_id=deficit_request.id,
        correlation_id=deficit_request.id,
        detail={
            "alerts": len(alerts),
            "languages": languages,
            "urgency": deficit_request.urgency,
            "radius_metres": deficit_request.radius_metres,
            "deferred_for_quiet_hours": deferred,
            "next_attempt_at": next_attempt_at.isoformat(),
        },
    )

    return alerts
