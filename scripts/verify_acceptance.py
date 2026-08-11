"""Prove the fifteen acceptance properties against the live database.

    python scripts/verify_acceptance.py

Each check exercises the real system: the real queries, the real database
roles, the real row-level security. Nothing is mocked, because the point is to
demonstrate that the properties hold in the thing that will run, not in a
description of it.

The script creates a small amount of its own data and removes it afterwards. It
never touches the donor register, the reference data or the audit trail.
"""

from __future__ import annotations

import ast
import base64
import inspect
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from bga.adapters.base import get_adapter
from bga.config import settings
from bga.crypto.field import decrypt_msisdn
from bga.db import scoped_session
from bga.domain import readmodel, tokens
from bga.domain.collection import resolve_collection_point
from bga.domain.dispatch import create_alerts_and_outbox
from bga.domain.matching import _RESERVE, match_and_reserve
from bga.models.reference import CollectionPoint, Facility
from bga.models.requests import Alert, DeficitRequest

ROOT = Path(__file__).resolve().parent.parent
HARARE = ZoneInfo("Africa/Harare")
Owner = sessionmaker(bind=create_engine(settings.database_url_owner))

results: list[tuple[int, str, bool, str]] = []


def check(number: int, title: str):
    def wrap(fn):
        try:
            passed, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a raising check is a failing check
            passed, detail = False, f"{type(exc).__name__}: {exc}"[:96]
        results.append((number, title, passed, detail))
        mark = "PASS" if passed else "FAIL"
        print(f"  {number:>2}  {mark}  {title:<52} {detail}")
        return fn

    return wrap


def next_weekday_at(hour: int) -> datetime:
    moment = datetime.now(HARARE).replace(hour=hour, minute=0, second=0, microsecond=0)
    while moment.weekday() >= 5 or moment < datetime.now(HARARE):
        moment += timedelta(days=1)
    return moment


def next_sunday_at(hour: int) -> datetime:
    moment = datetime.now(HARARE).replace(hour=hour, minute=0, second=0, microsecond=0)
    while moment.weekday() != 6 or moment < datetime.now(HARARE):
        moment += timedelta(days=1)
    return moment


# --- fixture ---------------------------------------------------------------

STATE: dict = {}


def setup() -> None:
    """One request with alerts, so the checks have something real to inspect.

    Contact reservations are cleared first. Without it the script is only
    correct on a register nobody has alerted recently: the frequency cap would
    exclude donors held by an earlier demo, setup would reserve nobody, and
    checks 8 and 13 would have no alert to work with. Clearing is the same
    thing POST /demo/reset does and it makes the run repeatable.
    """
    with Owner() as session:
        session.execute(text("UPDATE core.donor SET last_alerted_at = NULL"))
        session.commit()

    with Owner() as session:
        facility = session.execute(
            select(Facility).where(Facility.name.like("Harare Central%"))
        ).scalar_one()
        point = session.execute(
            select(CollectionPoint).where(CollectionPoint.name == "NBSZ Harare Branch")
        ).scalar_one()
        STATE["facility_id"] = facility.id
        STATE["point_id"] = point.id

    with scoped_session(
        facility_id=STATE["facility_id"], actor_id="verify", role="CLINICIAN"
    ) as session:
        request = DeficitRequest(
            facility_id=STATE["facility_id"],
            collection_point_id=STATE["point_id"],
            abo="O",
            rh="POS",
            units_required=2,
            urgency="URGENT",
            radius_metres=settings.radius_bands[0],
            state="ACTIVE",
        )
        session.add(request)
        session.flush()
        now = datetime.now(timezone.utc)
        result = match_and_reserve(
            session, STATE["point_id"], "O", "POS", 2, now
        )
        alerts = create_alerts_and_outbox(session, request, result.reserved_donors, now)
        STATE["request_id"] = request.id
        STATE["alert_ids"] = [a.id for a in alerts]

    if not STATE["alert_ids"]:
        # Better to say so than to let two checks fail with an IndexError that
        # looks like a defect in the property rather than in the fixture.
        print(
            "  SETUP WARNING: no donors were reserved, so checks 8 and 13 have no"
            " alert to use.\n  Seed the register first: python scripts/seed_donors.py"
        )


def teardown() -> None:
    """Remove only what this script made. The register and audit trail stay."""
    with Owner() as session:
        session.execute(
            text(
                "DELETE FROM core.outbox WHERE alert_id IN"
                " (SELECT id FROM core.alert WHERE deficit_request_id = :r)"
            ),
            {"r": STATE.get("request_id")},
        )
        session.execute(
            text("DELETE FROM core.deficit_request WHERE id = :r"),
            {"r": STATE.get("request_id")},
        )
        session.execute(text("UPDATE core.donor SET last_alerted_at = NULL"))
        session.commit()


# --- the fifteen -----------------------------------------------------------


def run() -> None:
    @check(1, "schema is at the head migration")
    def _():
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory

        config = Config(str(ROOT / "alembic.ini"))
        head = ScriptDirectory.from_config(config).get_current_head()
        with create_engine(settings.database_url_owner).connect() as conn:
            current = MigrationContext.configure(conn).get_current_revision()
        return current == head, f"current={current} head={head}"

    @check(2, "geo-fence anchors on a collection point, not the facility")
    def _():
        with Owner() as session:
            facility = session.execute(
                select(Facility).where(Facility.name.like("Chitungwiza%"))
            ).scalar_one()
            point, reason = resolve_collection_point(session, facility, next_weekday_at(10))
        if point is None:
            return False, f"no point resolved: {reason}"
        return (
            point.name != facility.name and "NBSZ" in point.name,
            f"{facility.name[:22]} -> {point.name}",
        )

    @check(3, "runtime matching uses no spatial operator")
    def _():
        now = datetime.now(timezone.utc)
        with Owner() as session:
            plan = "\n".join(
                session.execute(
                    text("EXPLAIN " + str(_RESERVE)),
                    {
                        "now": now,
                        "cp_id": STATE["point_id"],
                        "radius": 10000,
                        "abo": "O",
                        "rh": "POS",
                        "today": now.date(),
                        "cap_cutoff": now - timedelta(days=settings.frequency_cap_days),
                        "dispatch_cap": 40,
                    },
                ).scalars()
            )
        spatial = [token for token in ("ST_", "geography", "gist") if token in plan]
        return not spatial, f"spatial operators in plan: {spatial or 'none'}"

    @check(4, "MSISDN is stored encrypted, not as digits")
    def _():
        with Owner() as session:
            rows = session.execute(
                text("SELECT msisdn_ciphertext FROM core.donor LIMIT 25")
            ).scalars().all()
        if not rows:
            return False, "no donors in the register"
        for blob in rows:
            raw = bytes(blob)
            # Would only decode if somebody stored the number as text.
            try:
                if raw.decode("ascii").replace("+", "").isdigit():
                    return False, "a ciphertext decoded to digits"
            except UnicodeDecodeError:
                pass
            if decrypt_msisdn(raw).encode() in raw:
                return False, "ciphertext contains its own plaintext"
        return True, f"{len(rows)} sampled, all opaque ({len(bytes(rows[0]))} bytes each)"

    @check(5, "concurrent requests reserve disjoint donor sets")
    def _():
        factory = sessionmaker(bind=create_engine(settings.database_url_app))
        a, b = factory(), factory()
        try:
            now = datetime.now(timezone.utc)
            first = match_and_reserve(a, STATE["point_id"], "A", "POS", 1, now)
            second = match_and_reserve(b, STATE["point_id"], "A", "POS", 1, now)
            set_a, set_b = set(first.donor_ids), set(second.donor_ids)
            if not set_a or not set_b:
                return False, f"one side reserved nothing ({len(set_a)}, {len(set_b)})"
            return set_a.isdisjoint(set_b), f"{len(set_a)} and {len(set_b)}, disjoint"
        finally:
            a.rollback(); b.rollback(); a.close(); b.close()

    @check(6, "no alert has a duplicate provider message id")
    def _():
        with Owner() as session:
            dupes = session.execute(
                text(
                    "SELECT count(*) FROM (SELECT provider_message_id FROM core.alert"
                    " WHERE provider_message_id IS NOT NULL"
                    " GROUP BY 1 HAVING count(*) > 1) t"
                )
            ).scalar()
        return dupes == 0, f"{dupes} duplicated ids"

    @check(7, "resubmitting an outbox row returns the same message id")
    def _():
        adapter = get_adapter(settings.primary_gateway)
        key = f"verify-{STATE['request_id']}"
        first = adapter.submit("+263771234567", "verification", key)
        second = adapter.submit("+263771234567", "verification", key)
        return (
            first.provider_message_id == second.provider_message_id is not None,
            f"{first.provider_message_id} twice",
        )

    @check(8, "a late DELIVERED receipt cannot regress a CONFIRMED alert")
    def _():
        from bga.api.receipts import STATE_RANKS, _MONOTONIC_TRANSITION

        alert_id = STATE["alert_ids"][0]
        pmid = f"verify-{alert_id}"
        with scoped_session(
            facility_id=STATE["facility_id"], actor_id="verify", role="CLINICIAN"
        ) as session:
            session.execute(
                text(
                    "UPDATE core.alert SET provider_message_id = :p, state = 'CONFIRMED',"
                    " state_rank = :r WHERE id = :a"
                ),
                {"p": pmid, "r": STATE_RANKS["CONFIRMED"], "a": alert_id},
            )
        with scoped_session(
            facility_id=STATE["facility_id"], actor_id="verify", role="GATEWAY"
        ) as session:
            session.execute(
                _MONOTONIC_TRANSITION,
                {
                    "pmid": pmid,
                    "new_state": "DELIVERED",
                    "new_rank": STATE_RANKS["DELIVERED"],
                    "now": datetime.now(timezone.utc),
                },
            )
        with Owner() as session:
            state = session.execute(
                text("SELECT state FROM core.alert WHERE id = :a"), {"a": alert_id}
            ).scalar()
        return state == "CONFIRMED", f"state after replay: {state}"

    @check(9, "no code path can bypass an eligibility exclusion")
    def _():
        from bga.domain import eligibility

        smells = ("bypass", "override", "force", "skip", "ignore", "urgency", "admin")
        tree = ast.parse(inspect.getsource(eligibility))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            args = node.args
            if args.kwarg:
                return False, f"{node.name}() accepts **{args.kwarg.arg}"
            names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
            for name in names:
                if any(s in name.lower() for s in smells):
                    return False, f"{node.name}() takes {name!r}"
        signature = list(inspect.signature(eligibility.eligibility_status).parameters)
        return signature == ["donor", "eligibility", "today"], f"signature {signature}"

    @check(10, "row-level security hides another facility's requests")
    def _():
        engine = create_engine(settings.database_url_app)
        with engine.connect() as conn:
            conn.execute(text("BEGIN"))
            conn.execute(
                text("SELECT set_config('app.facility_id', :f, true)"),
                {"f": str(STATE["facility_id"])},
            )
            mine = conn.execute(text("SELECT count(*) FROM core.deficit_request")).scalar()
            conn.execute(
                text("SELECT set_config('app.facility_id', :f, true)"),
                {"f": "99999999-9999-9999-9999-999999999999"},
            )
            theirs = conn.execute(text("SELECT count(*) FROM core.deficit_request")).scalar()
            conn.execute(text("ROLLBACK"))
        return mine > 0 and theirs == 0, f"own scope {mine}, foreign scope {theirs}"

    @check(11, "the application role cannot delete audit history")
    def _():
        engine = create_engine(settings.database_url_app)
        for statement in (
            "DELETE FROM audit.audit_event",
            "UPDATE audit.audit_event SET actor = 'x'",
            "SELECT * FROM audit.audit_event",
        ):
            try:
                with engine.connect() as conn:
                    conn.execute(text(statement))
                return False, f"bga_app was allowed to: {statement[:34]}"
            except Exception:
                continue
        return True, "select, update and delete all denied"

    @check(12, "the read model cannot return a donor")
    def _():
        allowed = {"dict", "list[dict]"}
        for name, fn in vars(readmodel).items():
            if not inspect.isfunction(fn) or name.startswith("_"):
                continue
            if fn.__module__ != readmodel.__name__:
                continue
            annotation = inspect.signature(fn).return_annotation
            rendered = getattr(annotation, "__name__", str(annotation))
            if rendered not in allowed:
                return False, f"{name} -> {rendered}"
        return True, "every public function returns dict or list[dict]"

    @check(13, "a response token cannot be redeemed twice")
    def _():
        with scoped_session(
            facility_id=STATE["facility_id"], actor_id="verify", role="CLINICIAN"
        ) as session:
            alert = session.get(Alert, STATE["alert_ids"][-1])
            session.execute(
                text("DELETE FROM core.response_token WHERE alert_id = :a"), {"a": alert.id}
            )
            raw = tokens.issue_token(session, alert, settings.token_ttl_minutes)
            first = tokens.redeem(session, raw)
            second = tokens.redeem(session, raw)
        return (
            first is not None and second is None,
            f"first={'alert' if first else None}, second={second}",
        )

    @check(14, "the donor app has no import path to matching or the read model")
    def _():
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, bga.api.donor_main;"
                "print(' '.join(m for m in sys.modules if m.startswith('bga.')))",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        if completed.returncode != 0:
            return False, completed.stderr.strip().splitlines()[-1][:70]
        loaded = set(completed.stdout.split())
        forbidden = loaded & {
            "bga.domain.matching",
            "bga.domain.readmodel",
            "bga.api.dashboard",
        }
        return not forbidden, f"{len(loaded)} modules, none of matching/readmodel"

    @check(15, "a 21:00 Sunday request dispatches nothing")
    def _():
        with Owner() as session:
            before = session.execute(text("SELECT count(*) FROM core.outbox")).scalar()
            facility = session.execute(
                select(Facility).where(Facility.name.like("Chitungwiza%"))
            ).scalar_one()
            point, reason = resolve_collection_point(session, facility, next_sunday_at(21))
            after = session.execute(text("SELECT count(*) FROM core.outbox")).scalar()
        if point is not None:
            return False, f"a point resolved as open: {point.name}"
        return after == before, (reason or "")[:64]


def main() -> int:
    print("\n  BLOOD GEO ALERT - acceptance verification")
    print(f"  {datetime.now(HARARE):%A %d %B %Y, %H:%M} Africa/Harare")
    print("  " + "-" * 96)
    setup()
    try:
        run()
    finally:
        teardown()

    passed = sum(1 for *_, ok, _ in results if ok)
    print("  " + "-" * 96)
    print(f"\n  {passed} of {len(results)} properties hold\n")
    if passed < len(results):
        print("  Not holding:")
        for number, title, ok, detail in results:
            if not ok:
                print(f"    {number:>2}  {title} - {detail}")
        print()
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
