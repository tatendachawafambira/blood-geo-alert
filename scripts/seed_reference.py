"""Seed wards, facilities and collection points, then rebuild proximity.

    python scripts/seed_reference.py

Idempotent: every row is matched on its unique name and updated in place, so
running it twice changes nothing and running it after editing a coordinate
corrects the existing row rather than creating a second one.

Coordinates are from Appendix A of the execution plan. They are approximations
and were verified against a map before use - the NBSZ Harare Branch one in
particular, because it is the location a Harare reviewer is most likely to know.
"""

from __future__ import annotations

import sys

from sqlalchemy import cast, create_engine, func, select, text
from sqlalchemy.orm import Session

from bga.config import settings
from bga.domain.proximity import proximity_is_stale, rebuild_proximity
from bga.models.reference import CollectionPoint, Facility, Geography, Ward

# name, latitude, longitude, density (1 sparse .. 3 dense)
WARDS = [
    ("Harare CBD", -17.8292, 31.0522, 2),
    ("Mbare", -17.8620, 31.0330, 3),
    ("Highfield", -17.8790, 30.9950, 3),
    ("Glen View", -17.9070, 30.9750, 3),
    ("Budiriro", -17.9010, 30.9420, 3),
    ("Kuwadzana", -17.8390, 30.9300, 3),
    ("Mufakose", -17.8710, 30.9600, 2),
    ("Warren Park", -17.8180, 30.9770, 2),
    ("Dzivarasekwa", -17.7920, 30.9250, 2),
    ("Kambuzuma", -17.8470, 30.9840, 2),
    ("Southerton", -17.8560, 31.0090, 1),
    ("Belvedere", -17.8290, 31.0140, 1),
    ("Avondale", -17.7990, 31.0380, 1),
    ("Mabelreign", -17.7960, 30.9930, 1),
    ("Marlborough", -17.7770, 31.0000, 1),
    ("Mount Pleasant", -17.7690, 31.0510, 1),
    ("Borrowdale", -17.7520, 31.0870, 1),
    ("Greendale", -17.8060, 31.1210, 1),
    ("Eastlea", -17.8180, 31.0790, 1),
    ("Hatfield", -17.8720, 31.0790, 1),
    ("Waterfalls", -17.8890, 31.0430, 2),
    ("Msasa", -17.8480, 31.1160, 1),
    ("Mabvuku", -17.8290, 31.1620, 2),
    ("Tafara", -17.8210, 31.1750, 2),
    ("Epworth", -17.8900, 31.1470, 3),
    ("Chitungwiza", -18.0128, 31.0756, 3),
]

FACILITIES = [
    ("Parirenyatwa Group of Hospitals", -17.8175, 31.0405),
    ("Harare Central Hospital", -17.8630, 31.0090),
    ("Chitungwiza Central Hospital", -18.0100, 31.0700),
]

# name, kind, lat, lon, open_hour, close_hour, saturday, sunday, active
COLLECTION_POINTS = [
    ("NBSZ Harare Branch", "BRANCH", -17.8180, 31.0400, 8, 16, True, False, True),
    (
        "NBSZ Mobile Drive - Glen View Community Hall",
        "MOBILE",
        -17.9070,
        30.9750,
        9,
        15,
        True,
        False,
        True,
    ),
    # Deliberately inactive. Mobile drives are time-bounded and the system must
    # not route a donor to a drive that is not running (FR-COL-02). Leaving one
    # switched off in the seed data is what makes that testable.
    ("NBSZ Mobile Drive - Highfield", "MOBILE", -17.8790, 30.9950, 9, 15, False, False, False),
]


def point(lat: float, lon: float):
    return cast(func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326), Geography())


def seed_wards(session: Session) -> tuple[int, int]:
    created = updated = 0
    for name, lat, lon, _density in WARDS:
        ward = session.execute(select(Ward).where(Ward.name == name)).scalar_one_or_none()
        district = "Chitungwiza" if name == "Chitungwiza" else "Harare"
        if ward is None:
            session.add(Ward(name=name, district=district, centroid=point(lat, lon)))
            created += 1
        else:
            ward.district = district
            ward.centroid = point(lat, lon)
            updated += 1
    return created, updated


def seed_facilities(session: Session) -> tuple[int, int]:
    created = updated = 0
    for name, lat, lon in FACILITIES:
        facility = session.execute(
            select(Facility).where(Facility.name == name)
        ).scalar_one_or_none()
        if facility is None:
            session.add(Facility(name=name, latitude=lat, longitude=lon))
            created += 1
        else:
            facility.latitude, facility.longitude = lat, lon
            updated += 1
    return created, updated


def seed_collection_points(session: Session) -> tuple[int, int]:
    created = updated = 0
    for name, kind, lat, lon, open_h, close_h, sat, sun, active in COLLECTION_POINTS:
        cp = session.execute(
            select(CollectionPoint).where(CollectionPoint.name == name)
        ).scalar_one_or_none()
        values = dict(
            kind=kind,
            location=point(lat, lon),
            open_hour=open_h,
            close_hour=close_h,
            open_saturday=sat,
            open_sunday=sun,
            is_active=active,
        )
        if cp is None:
            session.add(CollectionPoint(name=name, **values))
            created += 1
        else:
            for key, value in values.items():
                setattr(cp, key, value)
            updated += 1
    return created, updated


def main() -> int:
    engine = create_engine(settings.database_url_owner)
    with Session(engine) as session, session.begin():
        w_new, w_upd = seed_wards(session)
        f_new, f_upd = seed_facilities(session)
        c_new, c_upd = seed_collection_points(session)
        session.flush()

        print(f"  wards             {w_new:>3} created, {w_upd:>3} updated")
        print(f"  facilities        {f_new:>3} created, {f_upd:>3} updated")
        print(f"  collection points {c_new:>3} created, {c_upd:>3} updated")

        rows = rebuild_proximity(session)
        print(f"\n  ward_collection_proximity rebuilt: {rows} rows")
        print(f"  proximity stale: {proximity_is_stale(session)}")

        # A ward with no proximity row can never be matched, whatever the
        # radius. Saying so here beats discovering it as a suspiciously small
        # eligible count on the day.
        orphans = session.execute(
            text(
                "SELECT w.name FROM core.ward w"
                " WHERE NOT EXISTS (SELECT 1 FROM core.ward_collection_proximity p"
                "                    WHERE p.ward_id = w.id) ORDER BY w.name"
            )
        ).scalars().all()
        if orphans:
            print(
                f"\n  NOTE: {len(orphans)} ward(s) have no collection point within "
                f"15 km and can never be matched:"
            )
            for name in orphans:
                print(f"        - {name}")
            print(
                "        Donors seeded there are registered but unreachable. That is a"
                "\n        real operational finding (SRS RSK-02), not a defect."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
