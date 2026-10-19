# Blood Geo Alert — Release 1.0

[![tests](https://github.com/tatendachawafambira/blood-geo-alert/actions/workflows/tests.yml/badge.svg)](https://github.com/tatendachawafambira/blood-geo-alert/actions/workflows/tests.yml)

Geo-fenced blood donor mobilisation for Zimbabwe.

A hospital records a blood deficit. The system resolves the nearest **open NBSZ
collection point** — a branch or a mobile drive, never the hospital — matches
eligible verified donors within a radius of that point, and dispatches a
tokenised SMS link so they can confirm attendance. Clinicians see aggregate
counts and never an individual donor.

Implements BGA-SRS-001 and BGA-SAD-001. `docs/HANDOVER.md` records honestly
what is built, what is partial, and what is deferred.

---

## Prerequisites

| | |
|---|---|
| Python | 3.11+ (developed on 3.13) |
| Docker | for PostgreSQL 16 + PostGIS 3.4 |
| Disk | ~2 GB for the image and the virtualenv |

No Node, no npm, no build step. Leaflet is vendored as two static files.

---

## Setup, from clone to running

### 1. Database

```powershell
docker compose up -d
docker compose exec db psql -U bga_owner -d bga -c "CREATE EXTENSION IF NOT EXISTS postgis;" -c "SELECT PostGIS_Version();"
```

You must see a version string.

### 2. Roles

Four roles, each with the narrowest grant that lets it do its job. Run as
`bga_owner`; change every password.

```sql
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS audit;

CREATE ROLE bga_app   LOGIN PASSWORD 'app_pw_change_me';
CREATE ROLE bga_audit LOGIN PASSWORD 'audit_pw_change_me';
CREATE ROLE bga_donor LOGIN PASSWORD 'donor_pw_change_me';
-- deliberately: none is the owner, none has BYPASSRLS

GRANT USAGE ON SCHEMA core TO bga_app, bga_donor;
ALTER DEFAULT PRIVILEGES IN SCHEMA core
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bga_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA core
  GRANT USAGE, SELECT ON SEQUENCES TO bga_app;

GRANT USAGE ON SCHEMA audit TO bga_audit;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT INSERT ON TABLES TO bga_audit;
-- bga_app gets NOTHING on audit. It cannot read, update or delete history.
```

⚠️ Run these **before** the migration. `ALTER DEFAULT PRIVILEGES` applies only
to tables created after it, so running the migration first means hand-granting
every table.

Then, **after** `alembic upgrade head`:

```sql
-- the audit writer must read the previous entry_hash to chain onto it
GRANT SELECT ON audit.audit_event TO bga_audit;

-- the donor response app: no grant at all on core.donor or core.outbox
GRANT SELECT, UPDATE ON core.response_token TO bga_donor;
GRANT SELECT, UPDATE ON core.alert          TO bga_donor;
GRANT SELECT ON core.collection_point       TO bga_donor;
GRANT SELECT (id, collection_point_id) ON core.deficit_request TO bga_donor;
CREATE POLICY donor_response ON core.alert TO bga_donor USING (true) WITH CHECK (true);
CREATE POLICY donor_response ON core.deficit_request TO bga_donor USING (true);
```

### 3. Environment

Generate the two secrets yourself and never let them reach git:

```powershell
python -c "import base64,os; print('BGA_FIELD_KEY='+base64.b64encode(os.urandom(32)).decode())"
python -c "import base64,os; print('BGA_BLIND_PEPPER='+base64.b64encode(os.urandom(32)).decode())"
```

`.env` in the project root, listed in `.gitignore`. Copy `.env.example`
as a starting point:

```
BGA_FIELD_KEY=...
BGA_BLIND_PEPPER=...
DATABASE_URL_OWNER=postgresql+psycopg://bga_owner:change_me_locally@127.0.0.1:5432/bga
DATABASE_URL_APP=postgresql+psycopg://bga_app:app_pw_change_me@127.0.0.1:5432/bga
DATABASE_URL_AUDIT=postgresql+psycopg://bga_audit:audit_pw_change_me@127.0.0.1:5432/bga
DATABASE_URL_DONOR=postgresql+psycopg://bga_donor:donor_pw_change_me@127.0.0.1:5432/bga
```

Check `git status` shows `.env` untracked before your first commit.

**Real SMS (optional).** The default gateway is `mock`, which sends nothing.
Two demonstration gateways deliver a genuine SMS: `android` (a handset running
[android-sms-gateway](https://github.com/capcom6/android-sms-gateway) on the
local network) and `twilio`. Both refuse any recipient not on `SMS_ALLOWLIST`,
and an empty allowlist blocks every send — the seeded register holds
structurally valid numbers that belong to real people. See `.env.example` for
the settings and `python scripts/check_sms_setup.py` to verify them.

### 4. Install and migrate

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .
pip install pytest

alembic upgrade head
alembic current          # must report (head)
```

### 5. Seed

```powershell
python scripts/seed_reference.py      # 26 wards, 3 facilities, 3 collection points
python scripts/seed_donors.py         # 800 synthetic donors
```

`seed_reference.py` is idempotent. `seed_donors.py` refuses if donors exist;
pass `--reset` to replace them. It uses a fixed random seed so the figures you
quote stay true — `--seed random` for a different draw.

### 6. Map assets

Leaflet 1.9.4 is vendored in `src/bga/static/vendor/` rather than fetched from
a CDN, so the map works offline. To refresh it:

```powershell
curl.exe -L -o src/bga/static/vendor/leaflet.js  https://unpkg.com/leaflet@1.9.4/dist/leaflet.js
curl.exe -L -o src/bga/static/vendor/leaflet.css https://unpkg.com/leaflet@1.9.4/dist/leaflet.css
```

---

## Running

Four processes. Two applications, two workers.

```powershell
uvicorn bga.api.main:app --port 8000            # clinician dashboard + gateway console
uvicorn bga.api.donor_main:app --port 8001      # donor response page (separate origin)
python -m bga.workers.dispatch_worker           # drains the outbox
python -m bga.workers.scheduler                 # escalation, notification, expiry
```

Add `--host 0.0.0.0` to reach the donor page from a phone on the same network.

| URL | |
|---|---|
| http://127.0.0.1:8000/ | raise a deficit request |
| http://127.0.0.1:8000/requests/{id} | the four cards and the map |
| http://127.0.0.1:8000/gateway | outbox, masked numbers, failure-rate control |
| http://127.0.0.1:8000/internal/health | liveness, outbox depth |
| http://127.0.0.1:8000/internal/metrics | counters and timings |
| http://127.0.0.1:8001/d/{token} | the donor page |

A second scheduler exits immediately — it is a single-active service guarded by
a Postgres advisory lock.

---

## Tests

```powershell
pytest -q                                        # 258 tests
pytest tests/test_matching.py -k concurrency -v  # the most important one
```

The suite drops and rebuilds `bga_test` from migrations on every run, so
"`alembic upgrade head` against an empty database produces the full schema" is
re-proven each time rather than once by hand. You do not need to create the
test database yourself.

---

## Acceptance verification

```powershell
python scripts/verify_acceptance.py
```

Checks the fifteen properties in `docs/HANDOVER.md` against the live database —
real queries, real roles, real row-level security. Exits non-zero if any fails.
It creates a request of its own and removes it afterwards; it never touches the
donor register, the reference data or the audit trail.

It does clear `donor.last_alerted_at` before running, so that the frequency cap
from an earlier demonstration does not starve its fixture.

---

## Demonstration

Three preset scenarios on the dashboard populate the form without submitting:

- **A** — Harare Central, O+, 4 units, urgent, next weekday 10:00
- **B** — Parirenyatwa, O−, 2 units, critical, next weekday 10:00
- **C** — Chitungwiza Central, O+, 4 units, critical, next Sunday 21:00 →
  refused, nothing sent

**Reset demo** clears requests, alerts, tokens, outbox, the dead letter queue
and every contact hold. It keeps donors, wards, facilities, collection points
and the audit trail.

The **Simulated time** field drives opening-hours and eligibility decisions.
Dispatch scheduling deliberately uses the real clock, so a scenario dated
tomorrow still sends today.

---

## Layout

```
src/bga/
  config.py        settings from .env
  db.py            engines, scoped_session (SET LOCAL for RLS)
  models/          reference, donor, requests, audit
  crypto/field.py  AES-256-GCM + HMAC blind index
  domain/          geo, proximity, eligibility, collection, matching,
                   consent, dispatch, tokens, audit, readmodel
  adapters/        base (Protocol), mock_sms, africastalking (skeleton),
                   android_sms + twilio_sms behind allowlist (demonstration)
  api/             main, dashboard, gateway_console, internal, receipts,
                   donor_main, donor, deps
  workers/         dispatch_worker, scheduler
alembic/versions/  schema, under migration
scripts/           seeding, reconciliation, acceptance verification
tests/             258 tests
```
