"""Generate synthetic donors.

    python scripts/seed_donors.py            # 800 donors
    python scripts/seed_donors.py 2000
    python scripts/seed_donors.py --reset    # delete existing donors first

Synthetic only. NFR-PRIV-05 forbids production personal data outside
production, and nothing here comes from a real person: the numbers are
generated, and they are encrypted before they touch the database exactly as a
real enrolment would be. No plaintext MSISDN is ever written.

The distributions are not decoration either. 4% under-18 without guardian
consent and 7% deferred are what make FR-DON-14 and FR-ELG-03 visible as real
numbers in the dashboard's exclusion breakdown, rather than rules nobody can
see working.
"""

from __future__ import annotations

import random
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Running "python scripts/seed_donors.py" puts scripts/ on sys.path but not the
# project root, so the sibling import below would fail. Works either way now,
# including "python -m scripts.seed_donors".
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from bga.config import settings
from bga.crypto.field import blind_index, encrypt_msisdn
from bga.domain.consent import CONSENT_TEXT, CONSENT_VERSION
from bga.domain.eligibility import compute_next_eligible_date, eligibility_status
from bga.models.donor import ConsentRecord, Donor, DonorEligibility
from bga.models.reference import Ward
from scripts.seed_reference import WARDS

DEFAULT_DONORS = 800

# Fixed by default so the register is reproducible. The plan asks you to write
# down how many O-negative donors exist and how many are eligible; with an
# unseeded generator those figures change every time anyone re-seeds, and the
# notes you rehearsed from become wrong. Pass --seed <n> for a different draw,
# or --seed random for a genuinely random one.
#
# Note this is sampling, not a guarantee: at 800 donors a group specified at 1%
# can easily land at 2%. The distribution is exact in the limit - verified at
# 200,000 draws - not in any one register.
DEFAULT_SEED = 20260811

# Real sub-Saharan African donor distribution.
BLOOD_GROUPS = [
    (("O", "POS"), 0.460),
    (("A", "POS"), 0.240),
    (("B", "POS"), 0.210),
    (("AB", "POS"), 0.050),
    (("O", "NEG"), 0.025),
    (("A", "NEG"), 0.010),
    (("B", "NEG"), 0.004),
    (("AB", "NEG"), 0.001),
]

LANGUAGES = [("sn", 0.55), ("en", 0.35), ("nd", 0.10)]

# Econet 77/78, NetOne 71, Telecel 73.
NETWORK_PREFIXES = ("71", "73", "77", "78")


def weighted(options):
    values = [value for value, _ in options]
    weights = [weight for _, weight in options]
    return random.choices(values, weights=weights, k=1)[0]


def make_msisdn(used: set[str]) -> str:
    """A valid Zimbabwean mobile number that is not already in the register."""
    while True:
        candidate = f"+263{random.choice(NETWORK_PREFIXES)}{random.randint(0, 9_999_999):07d}"
        if candidate not in used:
            used.add(candidate)
            return candidate


def build_donor(ward_id: int, today: date, used: set[str], reference_seq: int):
    msisdn = make_msisdn(used)
    abo, rh = weighted(BLOOD_GROUPS)
    sex = random.choice(("M", "F"))
    verified = random.random() < 0.75

    donor = Donor(
        # Encrypted here, in the application, before it reaches the database.
        # ADR-012: the database never sees the number or the key.
        msisdn_ciphertext=encrypt_msisdn(msisdn),
        msisdn_blind_index=blind_index(msisdn),
        # An NBSZ donor number exists only once the blood service has one on
        # file, which is what NBSZ_VERIFIED means. Reconciliation matches on it.
        donor_reference=f"NBSZ-{reference_seq:06d}" if verified else None,
        abo=abo,
        rh=rh,
        sex=sex,
        ward_id=ward_id,
        verification_state="NBSZ_VERIFIED" if verified else "SELF_DECLARED",
        availability="AVAILABLE" if random.random() < 0.92 else "PAUSED",
        preferred_language=weighted(LANGUAGES),
        # No guardian consent for any of them, so they are correctly excluded
        # and the exclusion is visible on the dashboard.
        age_band="UNDER_18" if random.random() < 0.04 else "EIGHTEEN_PLUS",
        guardian_consent_id=None,
        last_alerted_at=(
            datetime.now(timezone.utc) - timedelta(days=random.uniform(0, 14))
            if random.random() < 0.10
            else None
        ),
    )

    last_donation = (
        None if random.random() < 0.15 else today - timedelta(days=random.randint(1, 300))
    )
    next_eligible = compute_next_eligible_date(last_donation, sex)

    eligibility = DonorEligibility(
        last_donation_date=last_donation,
        # A never-donated donor is eligible now. The column is NOT NULL for a
        # reason: the reservation compares next_eligible_date <= :today, and a
        # NULL makes that predicate NULL rather than true, silently removing
        # the donor from every match.
        next_eligible_date=next_eligible or today,
        deferral_flag=random.random() < 0.07,
        deferral_review_date=None,
        donations_last_12m=0 if last_donation is None else random.randint(0, 3),
    )
    return donor, eligibility


def main(argv: list[str]) -> int:
    reset = "--reset" in argv
    count = next((int(a) for a in argv if a.isdigit()), DEFAULT_DONORS)
    today = date.today()

    if "--seed" in argv:
        value = argv[argv.index("--seed") + 1]
        seed = None if value == "random" else int(value)
    else:
        seed = DEFAULT_SEED
    random.seed(seed)
    print(f"  random seed: {seed if seed is not None else 'random (not reproducible)'}")

    engine = create_engine(settings.database_url_owner)
    with Session(engine) as session, session.begin():
        existing = session.execute(select(func.count()).select_from(Donor)).scalar() or 0
        if existing and not reset:
            print(
                f"  {existing} donors already exist. Re-run with --reset to replace them,"
                f"\n  or leave them as they are."
            )
            return 1
        if reset and existing:
            session.execute(
                text(
                    "TRUNCATE core.alert, core.outbox, core.response_token,"
                    " core.deficit_request, core.consent_record,"
                    " core.donor_eligibility, core.donor CASCADE"
                )
            )
            print(f"  removed {existing} existing donors and everything derived from them")

        wards = session.execute(select(Ward)).scalars().all()
        if not wards:
            print("  no wards. Run scripts/seed_reference.py first.")
            return 1

        density = {name: d for name, _lat, _lon, d in WARDS}
        ward_choices = [(w.id, density.get(w.name, 1)) for w in wards]

        used: set[str] = set()
        donors = []
        for i in range(count):
            ward_id = weighted(ward_choices)
            donor, eligibility = build_donor(ward_id, today, used, i + 1)
            session.add(donor)
            session.flush()
            eligibility.donor_id = donor.id
            session.add(eligibility)
            session.add(
                ConsentRecord(
                    donor_id=donor.id,
                    consent_version=CONSENT_VERSION,
                    granted_at=datetime.now(timezone.utc),
                    channel="ASSISTED",
                    text_presented=CONSENT_TEXT[donor.preferred_language],
                )
            )
            donors.append((donor, eligibility))
        session.flush()

        # --- summary ---------------------------------------------------
        groups = Counter(
            f"{d.abo}{'+' if d.rh == 'POS' else '-'}" for d, _ in donors
        )
        eligible = sum(1 for d, e in donors if eligibility_status(d, e, today)[0])
        reasons = Counter(
            eligibility_status(d, e, today)[1] for d, e in donors
            if not eligibility_status(d, e, today)[0]
        )
        verified = sum(1 for d, _ in donors if d.verification_state == "NBSZ_VERIFIED")

        print(f"\n  {len(donors)} donors created across {len(wards)} wards\n")
        print(f"  {'group':>6}  {'count':>5}  {'share':>6}")
        for group in ("O+", "A+", "B+", "AB+", "O-", "A-", "B-", "AB-"):
            n = groups.get(group, 0)
            print(f"  {group:>6}  {n:>5}  {n / len(donors):>5.1%}")

        print(f"\n  verified (NBSZ_VERIFIED)   {verified:>5}  {verified/len(donors):>5.1%}")
        print(f"  self-declared              {len(donors)-verified:>5}")
        print(f"\n  currently eligible         {eligible:>5}  {eligible/len(donors):>5.1%}")
        print(f"  not eligible               {len(donors)-eligible:>5}")
        for reason, n in reasons.most_common():
            print(f"      {reason.lower():<32} {n:>5}")

        o_neg = groups.get("O-", 0)
        o_neg_eligible = sum(
            1
            for d, e in donors
            if d.abo == "O" and d.rh == "NEG" and eligibility_status(d, e, today)[0]
        )
        print(
            f"\n  FOR THE PRESENTATION"
            f"\n      O-negative donors registered : {o_neg}"
            f"\n      O-negative currently eligible: {o_neg_eligible}"
            f"\n      all groups currently eligible: {eligible}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
