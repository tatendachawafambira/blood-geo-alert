"""NBSZ reconciliation import. FR-INT-02, FR-INT-04.

    python scripts/reconcile_import.py scripts/sample_reconciliation.csv
    python scripts/reconcile_import.py --write-sample

Release 1.0 runs a standalone register reconciled by scheduled file exchange
rather than a live API (ADR-007). Requiring an institution to modify its system
of record before it has seen any value from a pilot is the most reliable way to
ensure the pilot never starts.

THE IMPORT IS ALL OR NOTHING, and that is a safety property rather than a data
quality one. A half-applied deferral file leaves some donors still marked
alertable when the blood service has said they must not be contacted. Every row
is validated before anything is written, and any structural error rolls the
whole batch back and exits non-zero.

Blood service values win on conflict (FR-INT-04). This system is not the source
of truth for a laboratory result.
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from bga.config import settings
from bga.domain import audit
from bga.domain.eligibility import compute_next_eligible_date
from bga.models.donor import Donor, DonorEligibility

COLUMNS = (
    "donor_reference",
    "abo",
    "rh",
    "last_donation_date",
    "deferral_flag",
    "deferral_review_date",
)

ABO_VALUES = {"O", "A", "B", "AB"}
RH_VALUES = {"POS", "NEG"}
TRUE_VALUES = {"true", "t", "yes", "y", "1"}
FALSE_VALUES = {"false", "f", "no", "n", "0", ""}

SAMPLE_PATH = Path(__file__).resolve().parent / "sample_reconciliation.csv"


class RowError(Exception):
    """A structural problem. One of these rejects the entire file."""


def parse_date(value: str, field: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RowError(f"{field} is not an ISO date: {value!r}") from None


def parse_bool(value: str, field: str) -> bool:
    lowered = (value or "").strip().lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise RowError(f"{field} is not a boolean: {value!r}")


def validate(row: dict, line: int) -> dict:
    reference = (row.get("donor_reference") or "").strip()
    if not reference:
        raise RowError(f"line {line}: donor_reference is empty")

    abo = (row.get("abo") or "").strip().upper()
    if abo not in ABO_VALUES:
        raise RowError(f"line {line}: abo must be one of {sorted(ABO_VALUES)}, got {abo!r}")

    rh = (row.get("rh") or "").strip().upper()
    if rh not in RH_VALUES:
        raise RowError(f"line {line}: rh must be one of {sorted(RH_VALUES)}, got {rh!r}")

    try:
        last_donation = parse_date(row.get("last_donation_date", ""), "last_donation_date")
        review = parse_date(row.get("deferral_review_date", ""), "deferral_review_date")
        deferred = parse_bool(row.get("deferral_flag", ""), "deferral_flag")
    except RowError as exc:
        raise RowError(f"line {line}: {exc}") from None

    if last_donation and last_donation > date.today():
        raise RowError(f"line {line}: last_donation_date is in the future")

    return {
        "donor_reference": reference,
        "abo": abo,
        "rh": rh,
        "last_donation_date": last_donation,
        "deferral_flag": deferred,
        "deferral_review_date": review,
    }


def load(path: Path) -> list[dict]:
    """Read and validate the whole file before anything is written."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = set(COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise RowError(f"missing column(s): {sorted(missing)}")
        return [validate(row, line) for line, row in enumerate(reader, start=2)]


def apply_batch(session: Session, records: list[dict]) -> dict:
    applied = skipped = 0
    for record in records:
        donor = session.execute(
            select(Donor).where(Donor.donor_reference == record["donor_reference"])
        ).scalar_one_or_none()

        if donor is None:
            # Not a structural error. The blood service holds donors this pilot
            # never enrolled, and rejecting the file for that would mean no
            # import ever succeeds.
            skipped += 1
            continue

        # FR-INT-04: the blood service value wins. A laboratory result is not
        # something this system may argue with.
        donor.abo = record["abo"]
        donor.rh = record["rh"]
        donor.verification_state = "NBSZ_VERIFIED"

        eligibility = session.get(DonorEligibility, donor.id)
        if eligibility is None:
            eligibility = DonorEligibility(donor_id=donor.id, next_eligible_date=date.today())
            session.add(eligibility)

        eligibility.last_donation_date = record["last_donation_date"]
        eligibility.deferral_flag = record["deferral_flag"]
        eligibility.deferral_review_date = record["deferral_review_date"]
        # Recomputed through the real function, never duplicated here.
        eligibility.next_eligible_date = (
            compute_next_eligible_date(record["last_donation_date"], donor.sex)
            or date.today()
        )
        applied += 1

    session.flush()
    return {"applied": applied, "skipped": skipped}


def write_sample(session: Session) -> int:
    """Build a sample from donors that actually exist, so it imports cleanly."""
    donors = session.execute(
        select(Donor).where(Donor.donor_reference.is_not(None)).limit(8)
    ).scalars().all()
    if not donors:
        print("  no donors with an NBSZ reference. Run scripts/seed_donors.py first.")
        return 1

    with SAMPLE_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for index, donor in enumerate(donors):
            writer.writerow(
                {
                    "donor_reference": donor.donor_reference,
                    "abo": donor.abo,
                    "rh": donor.rh,
                    "last_donation_date": date.today().replace(month=1, day=15).isoformat(),
                    "deferral_flag": "true" if index % 4 == 0 else "false",
                    "deferral_review_date": (
                        date.today().replace(month=12, day=1).isoformat()
                        if index % 4 == 0
                        else ""
                    ),
                }
            )
    print(f"  wrote {len(donors)} rows to {SAMPLE_PATH}")
    return 0


def main(argv: list[str]) -> int:
    engine = create_engine(settings.database_url_owner)

    if "--write-sample" in argv:
        with Session(engine) as session, session.begin():
            return write_sample(session)

    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print("  usage: reconcile_import.py <file.csv> | --write-sample")
        return 2

    path = Path(paths[0])
    if not path.exists():
        print(f"  no such file: {path}")
        return 2

    # Validation happens entirely before the transaction opens. Nothing is
    # written unless every row is sound.
    try:
        records = load(path)
    except RowError as exc:
        print(f"\n  REJECTED: {exc}")
        print("  Nothing was written. The whole file is rejected, by design:")
        print("  a partly applied deferral import leaves donors incorrectly alertable.")
        return 1

    if not records:
        print("  the file has a header but no rows")
        return 1

    imported_at = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        result = apply_batch(session, records)

    audit.emit(
        action="RECONCILIATION_IMPORTED",
        actor="nbsz-reconciliation",
        target_class="DonorRegister",
        detail={
            "rows": len(records),
            "applied": result["applied"],
            "skipped_unknown_reference": result["skipped"],
            "source": path.name,
            # The freshness timestamp. It lives in the audit trail because no
            # column exists for it yet - see the note in the README/HANDOVER.
            "reconciled_at": imported_at.isoformat(),
        },
    )

    print(f"\n  {len(records)} rows validated")
    print(f"  {result['applied']} donors updated and set NBSZ_VERIFIED")
    print(f"  {result['skipped']} rows skipped (donor_reference not in this register)")
    print(f"  reconciled_at {imported_at.isoformat()} recorded in the audit trail")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
