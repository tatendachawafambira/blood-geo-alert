# Handover — Blood Geo Alert Release 1.0

For the engineer who inherits this. It is written to be honest rather than
flattering: what works, what half-works, and what was left out on purpose.

**What this release is.** The load-bearing parts of the system specified in
BGA-SRS-001, built correctly, with the leaves left off. The data model, the
concurrency, the encryption scheme, the security posture and the module
boundaries are all real. Whole features are absent. That trade was deliberate:
absent features are cheap to add, whereas a wrong data model or a naive
concurrency approach is not.

**What it is not.** It has not processed a real donor's data, it has not sent a
real SMS, and it has not been penetration tested. Do not put a live number into
it until the blocking items at the end of this document are closed.

---

## 1. Requirement groups

| Group | Status | Evidence or reason |
|---|---|---|
| FR-DON donor registry | **PARTIAL** | Schema, verification states, availability, age band, ward anchoring, dedup on blind index all implemented. The four enrolment channels (FR-DON-01) are not: donors arrive by seed or reconciliation only. |
| FR-CON consent and rights | **PARTIAL** | Consent capture with verbatim text in three languages, withdrawal, suppression surviving erasure (FR-CON-08). Withdrawal is a function, not yet reachable by SMS keyword, USSD or web (FR-CON-03). Subject access export not built. |
| FR-ELG eligibility | **IMPLEMENTED** | Pure module, no override path anywhere. 22 tests including boundary dates. Enforced independently in the reservation SQL. |
| FR-COL collection points | **IMPLEMENTED** | Registry, time-bounded mobile drives, open/closed computation, nearest-open resolution, refusal with a reason. No public holiday calendar (FR-COL-03) and no supervisor override (FR-COL-06). |
| FR-DEF deficit requests | **PARTIAL** | Creation, state machine, rejection path. No USSD origination (FR-DEF-03), no inventory trigger (FR-DEF-04), no stand-down message on closure (FR-DEF-06). |
| FR-GEO spatial matching | **IMPLEMENTED** | Precomputed proximity, radius bands, escalation, frequency cap under concurrency, dispatch cap. No ABO/Rh compatibility matrix (FR-GEO-05, correctly restricting to exact matches until NBSZ supplies one) and no rare-group panel (FR-GEO-04). |
| FR-MSG messaging | **PARTIAL** | Outbox, retry with jitter, circuit breaker, dead letter queue, quiet hours, GSM-7 single-segment enforcement, three languages. Only the mock adapter sends. No WhatsApp, no USSD, no voice. |
| FR-TOK token and response | **IMPLEMENTED** | 256-bit opaque tokens, hash at rest, single atomic redemption, indistinguishable failure page, 2.1 KB transfer, works without JavaScript. Rate limiting (FR-TOK-10) is not built. |
| FR-DSH dashboard | **PARTIAL** | Request form, four cards, live counters, map, aggregate-only read model. No authentication and therefore no MFA (FR-DSH-07) or idle timeout (FR-DSH-08). Not a PWA. |
| FR-ESC escalation | **PARTIAL** | Radius widening, facility notification, expiry, all idempotent under a leader lock. Notification is an audit event, not a message to a human. No voice escalation. |
| FR-ANA analytics | **PARTIAL** | Per-request aggregates, ward counts with small-cell suppression, `/internal/metrics`. No disaggregated reporting (FR-ANA-02), no export (FR-ANA-05). |
| FR-ADM administration | **DEFERRED** | Role and user tables exist in the schema so adding login is not a migration of live data. Nothing else: no login, no RBAC enforcement, no two-person authorisation. |
| FR-AUD audit | **IMPLEMENTED** | Append-only, hash-chained, separate role and connection, redaction at the writing layer. The app role holds no grant on the audit schema. Anomaly alerting (FR-AUD-07) not built. |
| FR-INT integration | **PARTIAL** | Atomic CSV reconciliation with blood-service precedence. Scheduled secure file exchange is manual. Webhook signature verification is a Protocol method the mock returns True from. |
| NFR performance | **PARTIAL** | Matching is an indexed integer join; EXPLAIN shows the composite range scan at 22,800 proximity rows. Never load tested at the stated volumes. |
| NFR availability | **DEFERRED** | Single host, no standby, no backups configured. ARK-01 in the architecture, and it applies. |
| NFR security | **PARTIAL** | Field encryption, RLS with FORCE, least-privilege roles, no secrets in source. No TLS, no MFA, no rate limiting, no dependency scanning in a pipeline, no penetration test. |
| NFR privacy | **PARTIAL** | Minimised collection, ward-level location only, aggregate-only read model, small-cell suppression, log redaction. Automatic retention purge (NFR-PRIV-04) is not built. |
| CR compliance | **DEFERRED** | Consent text drafted against the section 15 checklist. Every instrument — processing agreement, DPO appointment, controller licence, ethics clearance — is outstanding. |

---

## 2. Architecture decisions, and where each one lives

| ADR | Where |
|---|---|
| ADR-001 fence on the collection point | `domain/collection.py: resolve_collection_point` |
| ADR-004 ward centroid, no device location | `models/reference.py: Ward.centroid`; no geolocation call exists |
| ADR-005 counts, never individuals | `domain/readmodel.py` — validates its own return surface at import |
| ADR-006 opaque tokens | `domain/tokens.py: issue_token` |
| ADR-010 transactional outbox | `domain/dispatch.py: create_alerts_and_outbox` |
| ADR-011 precomputed proximity | `domain/proximity.py` — the only spatial operators in the system |
| ADR-012 AES-GCM + HMAC blind index | `crypto/field.py` |
| ADR-013 Postgres as the queue | `workers/dispatch_worker.py: _DRAIN` |
| ADR-014 monotonic alert state | `api/receipts.py: _MONOTONIC_TRANSITION` |
| ADR-015 atomic contact reservation | `domain/matching.py: _RESERVE` |
| ADR-016 isolated donor app | `api/donor_main.py` + the `bga_donor` role |
| ADR-017 RLS plus application scoping | `db.py: scoped_session`; policies in the initial migration |
| ADR-018 sync match, async dispatch | `api/dashboard.py: create_request` |

### The fifteen acceptance properties

`python scripts/verify_acceptance.py` — all fifteen pass as of this handover.

---

## 3. Deferred work, in priority order

| # | Item | Estimate | Why it matters |
|---|---|---|---|
| 1 | Authentication, MFA, RBAC enforcement | 3–5 days | Every dashboard route is currently unauthenticated. Blocks any real deployment. |
| 2 | TLS, HSTS, rate limiting on public endpoints | 1–2 days | FR-TOK-10 and NFR-SEC-01. The donor endpoint is enumerable today. |
| 3 | Real gateway adapter (Africa's Talking) | 2–3 days plus weeks of lead time | The skeleton and its seam exist. Sender ID registration is paperwork, not code, and is the long pole. |
| 4 | Webhook signature verification | 1 day | The mock returns True unconditionally. Without it anyone who guesses a message id can mark an alert delivered. |
| 5 | Automatic retention purge | 2 days | NFR-PRIV-04 requires enforcement, not intention. |
| 6 | Backups, standby, rehearsed restore | 2 days | ARK-01. Recovery objectives are not met today. |
| 7 | Donor enrolment channels (USSD, web, QR) | 3–4 days | Donors can only arrive by seed or reconciliation. |
| 8 | SMS keyword opt-out (`STOP`) end to end | 1 day | `withdraw_consent` exists; nothing routes an inbound message to it. The consent text promises this. |
| 9 | Freshness timestamp column and display | half a day | ADR-007 says a clinician relying on a count is entitled to know how old it is. Currently only in the audit trail. |
| 10 | Reconciliation as a scheduled job | 1 day | Today it is a command someone must run. |
| 11 | Compatibility matrix, rare-group panel | 2 days | Blocked on NBSZ supplying the matrix (OI-02). |
| 12 | WhatsApp, USSD, voice channels | 3–4 days each | Each is a new class behind the existing Protocol. |

---

## 4. Known gaps and technical debt

**Row-level security fails silently, and it has caught us four times.** An
unscoped query against `core.alert` or `core.deficit_request` returns zero rows
and raises nothing. The dispatch worker, the receipts webhook, the scheduler
and `/internal/health` each looked correct and reported an empty system. Any
new code touching those tables must open a `scoped_session`, and any aggregate
across facilities must iterate them. **Assume a zero is a scoping bug until you
have proved otherwise.**

**`WHERE id IN (SELECT … LIMIT n FOR UPDATE SKIP LOCKED)` does not bound an
update to n rows.** PostgreSQL may plan the sublink as the inner side of a
nested loop and re-execute it per outer row; `SKIP LOCKED` then hands back
different rows each time. Measured at 12 reserved against a cap of 10. It is
plan-dependent, so it looked fine at one table size and not at another. The
reservation uses `WITH cohort AS MATERIALIZED` and a test asserts the plan still
contains `CTE cohort`. Do not remove either.

**Cross-process state has no home.** The mock failure rate is shared through a
file in the temp directory. The circuit breaker is not shared at all —
`/internal/metrics` reports `"state": "unknown"` because the breaker lives in
the worker's memory. A small `system_state` table would fix both.

**No `notified_at` column.** Scheduler bookkeeping lives under a `scheduler`
key inside `matched_stats` JSONB. Reassign that dict, never mutate it —
SQLAlchemy does not track in-place JSONB changes and the facility would be
notified on every tick.

**`match_and_reserve` takes no band argument.** The scheduler widens one band by
temporarily narrowing `settings.radius_bands` around the call. Safe today
because the scheduler is single-threaded and holds the leader lock. Add a band
parameter before anything else calls it.

**The adapter used per message is not stored.** The gateway console infers it
from the `mock-` prefix. After a real failover nobody could tell which provider
carried which message. Add a column before a second real provider exists.

**Stale outbox locks depend on a timeout.** A killed worker leaves rows
`LOCKED`; they return to the queue after `worker_lock_timeout_seconds`
(default 300). Too short and two workers process one row; too long and messages
sit still after a crash.

**The reservation is stricter than the requirement on under-18 donors.** The SQL
requires `age_band = 'EIGHTEEN_PLUS'`, so an under-18 with valid guardian
consent is never alerted, though FR-DON-14 and `eligibility.py` would allow it.
Fails safe, but the two disagree.

**The annual donation limit is not in the reservation query.** `eligibility.py`
enforces `MAX_DONATIONS_PER_YEAR`; the SQL does not. Unreachable in practice —
a three-month interval caps a donor at four donations a year — but the claim
"eligibility is enforced in the query" has this one exception.

**Chitungwiza has no collection point within 15 km.** 21.9 km to the branch,
15.8 km to the Glen View drive, against a widest band of 10 km. Around 6% of
seeded donors live there and can never be matched. That is an operational
finding (SRS RSK-02), not a defect, and it is exactly the kind of thing the
pilot exists to surface.

**The Shona and Ndebele consent text is unreviewed.** Working translations of
approved English. They must be signed off by a first-language speaker and the
DPO before any real donor sees them.

**The message budget has 4 septets of headroom.** At the longest seeded point
name and a 43-character token, the Ndebele message is 156 of 160. A base URL
longer than 25 characters splits every message in two and doubles the cost per
mobilisation. `validate_templates()` fails the build if it happens.

---

## 5. Day one for a new engineer

1. Work through `README.md` end to end. Expect 30 minutes.
2. `pytest -q` — 253 tests. If they do not all pass, stop and fix that first.
3. `python scripts/verify_acceptance.py` — fifteen properties against the live
   database. This is the fastest way to understand what the system guarantees.
4. Read these four files, in this order. They are the system:
   - `domain/eligibility.py` — 60 lines, the one place where being wrong has a
     human consequence
   - `domain/matching.py` — the reservation, and why its exact shape matters
   - `domain/dispatch.py` and `workers/dispatch_worker.py` — the outbox
   - `db.py` — `scoped_session`, and why it is `SET LOCAL`
5. Read `docs/` ADRs 010, 011, 012, 015, 016 and 017 in BGA-SAD-001. They are
   binding, and reversing one needs a documented decision rather than a code
   review comment.

**Do not**: add a parameter that weakens an eligibility check; query
`core.alert` without a scope; rewrite the reservation or the outbox drain as a
SELECT followed by an UPDATE; or let the donor application acquire a route to
the donor register.
