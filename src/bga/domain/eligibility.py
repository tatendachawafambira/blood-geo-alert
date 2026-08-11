"""Donor eligibility. Pure functions: no database, no I/O, no clock.

This is the module where being wrong has a human consequence, so it is written
to be read. Roughly sixty lines, no cleverness, every rule visible.

FR-ELG-07 is absolute and structural: there is no argument, keyword, flag,
environment variable or branch anywhere below that can weaken or skip a check.
Not for administrators, not for CRITICAL requests, not for tests. If the
eligible pool is empty the correct response is to widen the radius or escalate
to the rare-group panel - never to relax a rule. Once an override exists, the
pressure of a critical request at two in the morning will find it, and a donor
safety decision moves from a documented clinical rule to a person under stress.

Note that the runtime matching query enforces the same rules independently in
SQL (ADR-008). That duplication is deliberate defence in depth, not an
oversight: this module answers "why was this donor excluded" for the dashboard,
while the query is what actually decides who gets contacted.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date

MALE_INTERVAL_MONTHS = 3
FEMALE_INTERVAL_MONTHS = 4
MAX_DONATIONS_PER_YEAR = 6

# Reason codes, in the order they are checked. The order matters and is part of
# the specification: a deferred donor who has also opted out reports OPTED_OUT,
# because that is the fact the operator most needs to see.
OPTED_OUT = "OPTED_OUT"
PAUSED = "PAUSED"
UNDER_18_NO_GUARDIAN_CONSENT = "UNDER_18_NO_GUARDIAN_CONSENT"
DEFERRED = "DEFERRED"
ANNUAL_LIMIT_REACHED = "ANNUAL_LIMIT_REACHED"
INTERVAL_NOT_ELAPSED = "INTERVAL_NOT_ELAPSED"

REASON_CODES = (
    OPTED_OUT,
    PAUSED,
    UNDER_18_NO_GUARDIAN_CONSENT,
    DEFERRED,
    ANNUAL_LIMIT_REACHED,
    INTERVAL_NOT_ELAPSED,
)


def _add_months(start: date, months: int) -> date:
    """Add whole months, clamping to the end of a short month.

    30 November plus three months is 28 February (29 in a leap year), not an
    invalid 30 February. Written out rather than pulled from dateutil, which
    is not a dependency of this project.
    """
    index = start.month - 1 + months
    year = start.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def compute_next_eligible_date(last_donation_date: date | None, sex: str) -> date | None:
    """The earliest date this donor may next donate.

    Returns None when there is no recorded donation - a donor who has never
    donated is not waiting on an interval. Callers persisting this must store a
    past date rather than NULL: the reservation query compares
    next_eligible_date <= :today, and NULL makes that predicate NULL rather
    than true, silently removing the donor from every match.
    """
    if last_donation_date is None:
        return None

    months = FEMALE_INTERVAL_MONTHS if sex == "F" else MALE_INTERVAL_MONTHS
    return _add_months(last_donation_date, months)


def eligibility_status(donor, eligibility, today: date) -> tuple[bool, str | None]:
    """Return (True, None) only if every check passes, else (False, reason).

    `donor` and `eligibility` are a Donor and its DonorEligibility row. Both
    are read, neither is modified.
    """
    if eligibility is None:
        # A donor with no eligibility row cannot be assessed. Refusing loudly
        # is correct: the alternative is to guess, and guessing here means
        # possibly alerting a deferred donor.
        raise ValueError(f"donor {getattr(donor, 'id', '?')} has no eligibility row")

    if donor.opt_out_at is not None:
        return False, OPTED_OUT

    # Fails closed: anything that is not explicitly AVAILABLE is treated as
    # paused, including an unset value.
    if donor.availability != "AVAILABLE":
        return False, PAUSED

    if donor.age_band == "UNDER_18" and donor.guardian_consent_id is None:
        return False, UNDER_18_NO_GUARDIAN_CONSENT

    if eligibility.deferral_flag:
        return False, DEFERRED

    if (eligibility.donations_last_12m or 0) >= MAX_DONATIONS_PER_YEAR:
        return False, ANNUAL_LIMIT_REACHED

    # None means never donated, which is not an interval to wait out.
    if eligibility.next_eligible_date is not None and eligibility.next_eligible_date > today:
        return False, INTERVAL_NOT_ELAPSED

    return True, None
