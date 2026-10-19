"""Put a real phone number into the donor register, so a real SMS can arrive.

    python scripts/set_demo_donor.py +263771234567
    python scripts/set_demo_donor.py +263771234567 --abo O --rh POS

Without this, every donor in the register holds a synthetic number and a
demonstration sends real messages into the void. This picks the donor the
matching query would reach FIRST for the given group - nearest ward to the
NBSZ Harare branch, eligible, verified, available - and replaces that one
record's number with yours.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not touch eligibility, deferral state or the frequency cap in any way
that could let an ineligible donor be alerted. It selects a donor who is
ALREADY matchable and swaps the contact number. FR-ELG-07 says there is no
path that bypasses an eligibility exclusion, and a demo helper is exactly the
kind of "just for testing" convenience that requirement exists to forbid.

It clears last_alerted_at for that one donor, which is not an eligibility
exclusion - it is the anti-harassment frequency cap (FR-GEO-06), and clearing
it for a synthetic record you are about to message yourself is the same thing
the acceptance script does in setup.
"""

from __future__ import annotations

import argparse
import sys
from uuid import uuid4
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from bga.config import settings  # noqa: E402
from bga.crypto.field import (  # noqa: E402
    blind_index,
    encrypt_msisdn,
    mask_msisdn,
    normalise_msisdn,
)

# The donor the matching query would reach first: closest ward to the chosen
# collection point, and passing every eligibility predicate the reservation
# applies. Ordering by distance means the 3 km band picks this donor up.
_CANDIDATE = text(
    """
    SELECT d.id, d.abo, d.rh, d.preferred_language, w.name AS ward, p.metres
      FROM core.ward_collection_proximity p
      JOIN core.donor d ON d.ward_id = p.ward_id
      JOIN core.donor_eligibility e ON e.donor_id = d.id
      JOIN core.ward w ON w.id = d.ward_id
     WHERE p.collection_point_id = :point_id
       AND d.abo = :abo AND d.rh = :rh
       AND d.verification_state = 'NBSZ_VERIFIED'
       AND d.availability = 'AVAILABLE'
       AND d.opt_out_at IS NULL
       AND d.age_band = 'EIGHTEEN_PLUS'
       AND e.deferral_flag = false
       AND e.next_eligible_date <= CURRENT_DATE
     ORDER BY p.metres
     LIMIT 1
    """
)

_APPLY = text(
    """
    UPDATE core.donor
       SET msisdn_ciphertext = :ct,
           msisdn_blind_index = :bi,
           last_alerted_at = NULL,
           updated_at = now()
     WHERE id = :donor_id
    """
)

# Does this number already sit on a donor, and is that donor still usable?
_EXISTING = text(
    """
    SELECT d.id, d.abo, d.rh, d.availability, w.name AS ward,
           COALESCE(e.deferral_flag, false) AS deferral_flag
      FROM core.donor d
      JOIN core.ward w ON w.id = d.ward_id
      LEFT JOIN core.donor_eligibility e ON e.donor_id = d.id
     WHERE d.msisdn_blind_index = :bi
    """
)

# Release a number from a donor who cannot be alerted, so it can be reassigned
# to one who can. The vacated record gets a parked number in the reserved
# +263 79x range, which no live handset uses.
_CLEAR = text(
    """
    UPDATE core.donor
       SET msisdn_ciphertext = :ct, msisdn_blind_index = :bi
     WHERE id = :donor_id
    """
)

_POINT = text(
    """
    SELECT id, name FROM core.collection_point
     WHERE name ILIKE :pattern AND is_active
     ORDER BY CASE WHEN kind = 'BRANCH' THEN 0 ELSE 1 END, name
     LIMIT 1
    """
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("msisdn", help="your number, e.g. +263771234567 or 0771234567")
    ap.add_argument("--abo", default="O", help="blood group to demo with (default O)")
    ap.add_argument("--rh", default="POS", choices=("POS", "NEG"),
                    help="rhesus type (default POS)")
    ap.add_argument("--point", default="%Harare%",
                    help="collection point name pattern (default %%Harare%%)")
    args = ap.parse_args()

    try:
        e164 = normalise_msisdn(args.msisdn)
    except Exception as exc:
        print(f"  that does not look like a Zimbabwean mobile number: {exc}")
        return 1

    engine = create_engine(settings.database_url_owner)
    Session = sessionmaker(bind=engine)

    with Session() as s, s.begin():
        point = s.execute(_POINT, {"pattern": args.point}).first()
        if point is None:
            print(f"  no collection point matching {args.point!r}")
            return 1

        ct, bi = encrypt_msisdn(e164), blind_index(e164)

        # Running this twice is normal, not an error. The first run put the
        # number on a donor; the second must find that same record and reset
        # its frequency cap, not refuse because the number is "taken". An
        # earlier version refused, which left the caller stuck with a donor
        # they could not re-arm and no way forward.
        existing = s.execute(_EXISTING, {"bi": bi}).first()

        if existing is not None:
            if existing.deferral_flag or existing.availability != "AVAILABLE":
                print(
                    f"\n  {mask_msisdn(e164)} is on a donor who cannot be alerted "
                    f"(deferred or paused).\n  Moving it to a matchable donor instead.\n"
                )
                # Park the vacated record on an unused number so the unique
                # blind index is free. 79x is outside the prefixes the seeder
                # uses, and it is not on the allowlist, so it can never be
                # messaged whatever matching decides.
                parked = f"+2637 9{uuid4().int % 10_000_000:07d}".replace(" ", "")
                s.execute(
                    _CLEAR,
                    {
                        "ct": encrypt_msisdn(parked),
                        "bi": blind_index(parked),
                        "donor_id": existing.id,
                    },
                )
            else:
                s.execute(_APPLY, {"ct": ct, "bi": bi, "donor_id": existing.id})
                print(f"\n  Collection point : {point.name}")
                print(f"  Donor            : {existing.id}   (already held your number)")
                print(f"  Ward             : {existing.ward}")
                print(
                    f"  Group            : {existing.abo}"
                    f"{'+' if existing.rh == 'POS' else '-'}"
                )
                print(f"  Number           : {mask_msisdn(e164)}   <- your handset")
                print(
                    "\n  Frequency cap cleared. Raise a request for this group and\n"
                    "  the message will reach you.\n"
                )
                return 0

        donor = s.execute(
            _CANDIDATE,
            {"point_id": point.id, "abo": args.abo, "rh": args.rh},
        ).first()
        if donor is None:
            print(
                f"  no eligible {args.abo}{'+' if args.rh == 'POS' else '-'} donor "
                f"near {point.name}. Try another group, or re-seed."
            )
            return 1

        s.execute(_APPLY, {"ct": ct, "bi": bi, "donor_id": donor.id})

    print(f"\n  Collection point : {point.name}")
    print(f"  Donor            : {donor.id}")
    print(f"  Ward             : {donor.ward}  ({donor.metres/1000:.1f} km from the point)")
    print(f"  Group            : {donor.abo}{'+' if donor.rh == 'POS' else '-'}")
    print(f"  Language         : {donor.preferred_language}")
    print(f"  Number           : {mask_msisdn(e164)}   <- your handset")
    print(
        "\n  This donor is inside the 3 km band and passes every eligibility\n"
        "  predicate, so the next matching request for this group will reach it.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
