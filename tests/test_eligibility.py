"""Eligibility rules. Pure functions, so no database is involved.

Every test constructs a donor and asks the real function. Nothing here bypasses
a check, and there is no argument that would let it - which is the point of
FR-ELG-07 and is itself asserted at the bottom of this file.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import date, datetime, timezone

import pytest

from bga.domain import eligibility as elig
from bga.domain.eligibility import compute_next_eligible_date, eligibility_status
from bga.models.donor import Donor, DonorEligibility

TODAY = date(2026, 8, 11)


def make_donor(**overrides) -> Donor:
    """An eligible adult donor. Tests make one thing wrong at a time."""
    defaults = dict(
        availability="AVAILABLE",
        age_band="EIGHTEEN_PLUS",
        guardian_consent_id=None,
        opt_out_at=None,
        sex="M",
    )
    return Donor(**{**defaults, **overrides})


def make_eligibility(**overrides) -> DonorEligibility:
    defaults = dict(
        last_donation_date=None,
        next_eligible_date=None,
        deferral_flag=False,
        donations_last_12m=0,
    )
    return DonorEligibility(**{**defaults, **overrides})


def months_before(months: int) -> date:
    """A donation date `months` whole months before TODAY."""
    month_index = TODAY.month - 1 - months
    year = TODAY.year + month_index // 12
    return date(year, month_index % 12 + 1, TODAY.day)


# --- compute_next_eligible_date -------------------------------------------


def test_never_donated_has_no_next_eligible_date():
    assert compute_next_eligible_date(None, "M") is None
    assert compute_next_eligible_date(None, "F") is None


def test_male_interval_is_three_months():
    assert compute_next_eligible_date(date(2026, 1, 15), "M") == date(2026, 4, 15)


def test_female_interval_is_four_months():
    assert compute_next_eligible_date(date(2026, 1, 15), "F") == date(2026, 5, 15)


def test_month_end_clamps_rather_than_overflowing():
    # 30 November + 3 months is 28 February, not an invalid 30 February.
    assert compute_next_eligible_date(date(2025, 11, 30), "M") == date(2026, 2, 28)
    # And 29 February in a leap year.
    assert compute_next_eligible_date(date(2023, 11, 30), "M") == date(2024, 2, 29)


# --- interval -------------------------------------------------------------


def test_male_two_months_after_donation_is_not_eligible():
    donated = months_before(2)
    donor = make_donor(sex="M")
    e = make_eligibility(
        last_donation_date=donated,
        next_eligible_date=compute_next_eligible_date(donated, "M"),
    )
    assert eligibility_status(donor, e, TODAY) == (False, elig.INTERVAL_NOT_ELAPSED)


def test_male_four_months_after_donation_is_eligible():
    donated = months_before(4)
    donor = make_donor(sex="M")
    e = make_eligibility(
        last_donation_date=donated,
        next_eligible_date=compute_next_eligible_date(donated, "M"),
    )
    assert eligibility_status(donor, e, TODAY) == (True, None)


def test_female_three_months_after_donation_is_not_eligible():
    donated = months_before(3)
    donor = make_donor(sex="F")
    e = make_eligibility(
        last_donation_date=donated,
        next_eligible_date=compute_next_eligible_date(donated, "F"),
    )
    assert eligibility_status(donor, e, TODAY) == (False, elig.INTERVAL_NOT_ELAPSED)


def test_female_five_months_after_donation_is_eligible():
    donated = months_before(5)
    donor = make_donor(sex="F")
    e = make_eligibility(
        last_donation_date=donated,
        next_eligible_date=compute_next_eligible_date(donated, "F"),
    )
    assert eligibility_status(donor, e, TODAY) == (True, None)


def test_eligible_exactly_on_the_boundary_date():
    # next_eligible_date == today must be eligible: the rule is "not before".
    e = make_eligibility(next_eligible_date=TODAY)
    assert eligibility_status(make_donor(), e, TODAY) == (True, None)


def test_never_donated_is_eligible():
    assert eligibility_status(make_donor(), make_eligibility(), TODAY) == (True, None)


# --- absolute exclusions --------------------------------------------------


def test_opted_out_beats_everything():
    donor = make_donor(opt_out_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    # Every other rule also failing: opt-out is still the reported reason.
    e = make_eligibility(
        deferral_flag=True,
        donations_last_12m=99,
        next_eligible_date=date(2099, 1, 1),
    )
    assert eligibility_status(donor, e, TODAY) == (False, elig.OPTED_OUT)


def test_deferral_beats_interval_and_annual_limit():
    e = make_eligibility(deferral_flag=True, donations_last_12m=99)
    assert eligibility_status(make_donor(), e, TODAY) == (False, elig.DEFERRED)


def test_deferred_donor_is_excluded_even_when_otherwise_perfectly_eligible():
    e = make_eligibility(deferral_flag=True)
    assert eligibility_status(make_donor(), e, TODAY) == (False, elig.DEFERRED)


def test_paused_donor_is_not_eligible():
    donor = make_donor(availability="PAUSED")
    assert eligibility_status(donor, make_eligibility(), TODAY) == (False, elig.PAUSED)


def test_unset_availability_fails_closed():
    donor = make_donor(availability=None)
    assert eligibility_status(donor, make_eligibility(), TODAY) == (False, elig.PAUSED)


# --- age ------------------------------------------------------------------


def test_under_18_without_guardian_consent_is_not_eligible():
    donor = make_donor(age_band="UNDER_18", guardian_consent_id=None)
    assert eligibility_status(donor, make_eligibility(), TODAY) == (
        False,
        elig.UNDER_18_NO_GUARDIAN_CONSENT,
    )


def test_under_18_with_guardian_consent_is_eligible():
    donor = make_donor(age_band="UNDER_18", guardian_consent_id=uuid.uuid4())
    assert eligibility_status(donor, make_eligibility(), TODAY) == (True, None)


# --- annual limit ---------------------------------------------------------


def test_annual_limit_reached_is_not_eligible():
    e = make_eligibility(donations_last_12m=elig.MAX_DONATIONS_PER_YEAR)
    assert eligibility_status(make_donor(), e, TODAY) == (False, elig.ANNUAL_LIMIT_REACHED)


def test_one_below_the_annual_limit_is_eligible():
    e = make_eligibility(donations_last_12m=elig.MAX_DONATIONS_PER_YEAR - 1)
    assert eligibility_status(make_donor(), e, TODAY) == (True, None)


# --- integrity ------------------------------------------------------------


def test_missing_eligibility_row_refuses_loudly_rather_than_guessing():
    with pytest.raises(ValueError):
        eligibility_status(make_donor(), None, TODAY)


def test_eligibility_status_takes_exactly_three_arguments():
    """FR-ELG-07, asserted rather than trusted.

    The only inputs are the donor, their eligibility row, and the date. There
    is nowhere to pass an urgency, a role, or a flag, so no caller can ask for
    a weaker answer.
    """
    signature = inspect.signature(eligibility_status)
    assert list(signature.parameters) == ["donor", "eligibility", "today"]
    assert not any(
        p.default is not inspect.Parameter.empty for p in signature.parameters.values()
    ), "an optional parameter on this function is a bypass waiting to be used"


def test_no_function_in_the_module_accepts_a_bypass_argument():
    """Checks the parse tree, not the prose.

    A grep would trip over this module's own prohibition docstring. This walks
    every function definition and looks at real parameter names, so it fails
    only if someone actually adds `force=`, `skip_checks=` or similar.
    """
    smells = ("bypass", "override", "force", "skip", "ignore", "urgency", "role", "admin")
    tree = ast.parse(inspect.getsource(elig))

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        args = node.args
        names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
        if args.vararg:
            names.append(args.vararg.arg)
        if args.kwarg:
            # **kwargs would let a caller smuggle anything in.
            raise AssertionError(f"{node.name}() accepts **{args.kwarg.arg}")
        for name in names:
            assert not any(s in name.lower() for s in smells), (
                f"{node.name}() takes a parameter named {name!r}, which looks like"
                " an eligibility bypass (FR-ELG-07)"
            )
