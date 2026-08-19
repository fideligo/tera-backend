# Decisions

One entry per non-obvious choice, per deviation from `BUILD_SPEC.md`, and per dependency.
Newest section last. Phase 1 only so far.

---

## Deviations from BUILD_SPEC.md

Each of these adds to or departs from the spec. Where a deviation resolves a conflict between the
spec text and a section-2 invariant, the invariant won and the reasoning is spelled out.

### D1. `app_user` table and `monitoring_episode.reviewing_clinician_id`

**Spec position.** §4.1 lists no user entity and no reviewing-clinician column. §4.5 requires
"role claims `patient`, `clinician`, `admin`" and "clinician access scoped to episodes where they
are the reviewing professional".

**Problem.** The scoping requirement is not implementable without somewhere to hold a clinician
identity and a link from an episode to the clinician reviewing it.

**Decision.** Added `app_user` (subject, password hash, role, clinic, optional patient link) and
`monitoring_episode.reviewing_clinician_id`. A `CHECK` keeps the patient link and the role
consistent: a `patient` principal must name a patient, a clinician or admin must not.

### D2. `calibration.established_at` and `calibration.superseded_at`

**Spec position.** §4.1 gives `calibration` a status and a `superseded_by` self-FK, but no
timestamps.

**Problem.** Invariant 4 requires that "every estimate references the calibration in force **at
capture time**", and §4.7 names `test_estimate_references_calibration_in_force_at_capture_time`.
Without temporal columns the only resolvable question is "which calibration is active *now*",
which gives the wrong answer whenever a recalibration lands between capture and upload — the
session would be interpreted against a baseline that did not exist when it was recorded.

**Decision.** Added both columns. Resolution is
`established_at <= session.started_at AND (superseded_at IS NULL OR superseded_at > started_at)`,
most recent first. `CHECK` constraints keep `status`, `superseded_by_id` and `superseded_at`
mutually consistent.

### D3. `calibration_source_session` join table

**Spec position.** §4.1 records only `n_sessions` on `calibration`.

**Problem.** `n_sessions >= 3` is required as a database `CHECK`, and §4.4 is explicit that the
backend does not trust the client. With no record of *which* sessions formed the baseline, the
server would have to accept a client-supplied count and a client-supplied
`baseline_mean_ms`/`baseline_sd_ms`. A handset that can write its own baseline can make any later
session read as stable.

**Decision.** The calibration request names session ids; the server loads them, reduces each to
its trimmed-mean PTT, computes the baseline itself, and records the contributing sessions with
the PTT each contributed. `POST /v1/calibrations` has no field for a baseline value, and unknown
fields are rejected.

### D4. `synthetic BOOLEAN NOT NULL DEFAULT false` on every clinical table

**Spec position.** §4.1's column lists do not mention it; §4.6 and invariant 9 require that
"every seeded row must carry `synthetic: true` and the API must surface that flag".

**Decision.** A column on every entity table plus `synthetic` and `synthetic_notice` on every
response that carries a stored row. Real data is the default; being synthetic takes a deliberate
act. `synthetic_notice` is populated only when the flag is true, so it never becomes background
noise a reader learns to skip.

### D5. `POST /v1/auth/token` and `POST /v1/auth/refresh`

**Spec position.** §4.2's endpoint table has no auth endpoint; §4.5 mandates "OAuth2 with
short-lived JWT access tokens plus refresh".

**Decision.** Added both. OAuth2 password grant, 15-minute access tokens, 14-day refresh tokens.
The `typ` claim is checked on decode, so a refresh token presented as an access token is refused —
otherwise the long-lived token would work everywhere and the short access TTL would be decorative.

### D6. `session_nonce` table

**Spec position.** Not in §4.1. §4.2/§4.5 require single-use nonces with a TTL; §4 rules out Redis
without justification.

**Decision.** Nonces live in Postgres. Single-use has to hold across every API process, and an
in-memory store would allow the same nonce to be spent once per worker. The row is locked
`FOR UPDATE` before the used-check so two concurrent submissions cannot both pass. Not a clinical
row, so `used_at` is written on consumption and expired rows can be purged.

### D7. `trend_estimate.deviation_state`

**Spec position.** §4.1 lists direction, magnitude, confidence and `computed_at`.

**Problem.** §4.3 distinguishes `possible_deviation` from `persistent`, and only the latter
requests a cuff. Persistence depends on whether a *repeat within the window* also deviated, which
is a fact about the moment of ingest. Recomputing it on read would give different answers as
later sessions arrive.

**Decision.** Persist it. A `CHECK` ties it to `direction`, so "stable but persistent" cannot be
constructed.

### D8. `protocol_params.persistence_window_hours`

**Spec position.** §4.1 lists cuff schedule, deviation multiplier `k` and minimum beat count.
§4.3 requires "a repeat session within the configured window" but never names the setting.

**Decision.** Added the key, default 48 hours, documented in `app/config.py`. Chosen so a patient
measuring once or twice daily can realistically produce the repeat, without the pair spanning so
much time that the two sessions describe different physiological states. Invariant 10 forbids
leaving it as a literal.

### D9. `cuff_reading.source = 'photograph'` is rejected by the API

**Spec position.** §4.1 defines the enum as `(manual_entry, photograph)` with an `ocr_confidence`
column. §8 puts seven-segment OCR out of scope: "manual entry only for now".

**Decision.** The value and the column exist in the schema for completeness, and the route returns
422 for both. Accepting `photograph` would imply a capability that does not exist and would
persist rows whose `ocr_confidence` is unpopulated — invariant 9 in a small way.

### D10. Rejection reason enum

**Spec position.** §4.1 has a `rejection_reason` column; no values are enumerated anywhere.

**Decision.** Eleven values covering the device-side quality gate, the server-side plausibility
gate, the red-flag path and the no-calibration case. Every value describes a *system* condition.
None describes the patient, because a rejected session says nothing about them (invariant 6).

### D11. `cuff_reading.corrects_id`

**Spec position.** Not in §4.1's column list. Invariant 5 says "corrections are new rows
referencing the original".

**Decision.** A self-FK, so the reference is real rather than implied. Both rows stay on the
timeline; nothing is replaced.

### D12. `409` on a duplicate session id returns the stored body

§4.2 says "409 duplicate session_id — return the stored result unchanged". That is unusual — an
idempotent endpoint more commonly replays `200`/`201`. The spec is explicit, so it is implemented
exactly as written: status 409, body identical to the original response. Noted in `docs/api.md`
so a client author is not surprised.

The response is **rebuilt from the stored rows**, not replayed from a cached body, so a replay
cannot diverge from what is actually on the record.

### D13. Idempotency is checked before the nonce is spent

§4.2 gives no ordering. Checking the nonce first would mean a client retrying after a dropped
response gets 428 for a nonce it already spent successfully, making a lost response
unrecoverable. Idempotency first; the retry gets its stored result.

### D14. `Idempotency-Key` must equal `session_id`

§4.2 shows `Idempotency-Key: <session_id>` but does not say it is enforced. It is: a mismatch is
400. Otherwise a client could deduplicate two different captures under one key, or submit one
capture twice under two keys.

---

## Conflicts resolved in favour of an invariant

Section 2 says an invariant wins a conflict with the rest of the spec. Two arose.

### C1. The achieved-rate check applies to completed sessions only

**The conflict.** §4.4 lists "achieved rates below the device profile's qualified band" as a 422,
without qualifying it by status. But a session rejected for `sensor_rate_below_qualified` reports
low rates *precisely because that is why it failed*. Applying the check to it would return 422 and
**discard** the session.

**Resolution.** Invariant 3 — "rejected sessions are retained, never discarded" — wins. The rate
check gates completed sessions only. Every structural check (array length bound, PTT plausibility,
beat accounting, quality-field presence) still applies to every payload, because invariant 2 does
not bend for a rejected session.

Covered by `test_rejected_session_with_low_rates_is_still_stored`.

### C2. Supersession writes to the old calibration row

**The conflict.** Invariant 4 says recalibration "never mutates history", and invariant 5 makes
clinical records append-only — yet the same invariant 4 specifies `status active/superseded` and a
`superseded_by` self-FK, which can only be set by updating the old row.

**Resolution.** Read as: the *baseline* is immutable; the supersession bookkeeping is not. A
PL/pgSQL trigger (`tera_calibration_history_guard`) permits `status`, `superseded_by_id` and
`superseded_at` to change and rejects any change to `patient_id`, `device_profile_id`,
`reference_cuff_reading_id`, `baseline_mean_ms`, `baseline_sd_ms`, `n_sessions`, `established_at`
or `synthetic`. It also blocks `DELETE` and makes supersession one-way: a superseded calibration
cannot be reactivated, because that would let an estimate be reinterpreted against a baseline that
had already been retired.

Covered by `test_recalibration_supersedes_and_does_not_mutate`,
`test_calibration_baseline_cannot_be_mutated_at_database_level` and `test_supersession_is_one_way`.

---

## Design decisions

### Append-only enforced by database triggers, not by the absence of routes

Invariant 5 is easy to satisfy superficially — just do not write a `DELETE` route. But a
migration, a console session or a future developer's convenience helper can all issue an `UPDATE`.
`tera_append_only_guard` is attached to every clinical table and raises on `UPDATE` or `DELETE`.
`test_clinical_tables_reject_update_and_delete` is parametrised over the table list, so a table
added without a trigger fails there.

The seeder's `--reset` uses `TRUNCATE`, which is not a row-level operation and so is not caught by
the triggers. That is why `--reset` is a development-only CLI flag and is not exposed over HTTP.

### The `superseded_by_id` foreign key is `DEFERRABLE INITIALLY DEFERRED`

The partial unique index allows one active calibration per patient per device and is checked per
statement. So the old row must be marked superseded *before* the new row is inserted, which means
it briefly points at an id that does not yet exist. Deferring the FK check to commit is the only
way to satisfy both constraints. Found by the seeder failing on the recalibration step.

### The `ptt_ms` ceiling lives in a migration as well as in config

`PlausibilitySettings.max_ptt_array_length` (default 300) is what the API enforces and can be
lowered freely. `ck_session_ptt_array_length_bounded` fixes a structural ceiling of 300 in the
database. Raising the config above it requires a migration — deliberately, because widening the
channel invariant 2 exists to protect should not be an environment-variable change.
`test_ptt_array_db_ceiling_matches_config` fails if the two drift.

### One interval per usable beat, enforced

`len(ptt_ms) == n_beats_usable` is a hard check. A mismatch means the array and the counts describe
different things and there is no safe way to guess which is right.

### The quality term in the confidence heuristic takes the worst limb, not the average

Averaging SNR, motion and dropped-frame scores would let a good SNR hide a capture ruined by
movement. `min()` is the escalation-biased choice (invariant 7). The whole formula is capped
strictly below 1.0 so no response can be read as certainty, and it is labelled a heuristic in the
response itself (`confidence_notice`).

### Persistence requires the same direction

§4.3 says persistent means "a repeat session within the configured window also deviates". A
session reading high followed by one reading low is instability, not a trend, and requesting a
cuff on that pairing would train the patient to ignore the request. Covered by
`test_opposite_direction_repeat_is_not_persistent`.

### A zero-variance baseline is refused

Three or more calibration sessions with identical session PTT means the device is not resolving
real variation. A baseline with zero spread would make every later session read as an infinite
deviation. Invariant 7: escalate rather than record a reference that cannot mean anything.

### All user-facing copy lives in `app/services/language.py`

Invariant 6 is about what the system *says*. Keeping every badge, action message, interpretation
and rejection explanation in one module means `test_no_diagnostic_or_medication_advice_language`
can enumerate all of them against a deny-list, rather than hoping a reviewer notices a sentence
added inline in a route later. The deny-list catches the obvious failures; the real protection is
that the copy is in one reviewable place.

### Logs are scrubbed by a formatter, not by convention

`RedactingJsonFormatter` matches field names against a deny-list, including as substrings, and
redacts recursively through nested structures. A developer who writes
`extra={"systolic_mmhg": 148}` gets `[redacted]`, not a leak. §4.5's rule is enforced rather than
documented.

### Cross-tenant access returns 404, not 403

A patient probing another patient's episode id, or a clinician who is not the reviewer, gets 404.
403 would confirm the id names a real episode — a small but free disclosure about someone else.

### Timeline record types are pairwise disjoint

§4.2 requires estimates and cuff readings to be "distinct types with distinct field sets". The
badges are named differently too (`cuff_badge` vs `estimate_badge`) so neither can be substituted
for the other. Three field sets are shared deliberately and are documented in
`app/schemas/timeline.py`: structural fields every timeline item has, `session_id` on the two
session-derived types, and the common shape of the three event types.

### The clinician summary is appended on every generation

Rather than updating a single row's `viewed_at`. Each `GET .../summary` inserts a
`clinician_summary` row with the rendered contents, and `viewed_at` is set only when a clinician is
the caller. The result is a record of what was actually on screen and when, which is both
append-only and more useful than the latest rendering alone. `delivered_at` stays null —
notification delivery is out of scope (§8).

### `received_at` is settable by the seeder and by nothing else

`ingest.submit()` takes an optional `received_at` so a demonstration episode has upload times
matching its capture times. The HTTP route never passes it: a client must not be able to backdate
when the server received something.

### seed-demo goes through the real ingest path

`app/cli/seed_demo.py` calls `ingest.submit()` for every session, so the seeded episode exercises
the same plausibility gate, calibration resolution and deviation engine as a real device. A seeder
that wrote `trend_estimate` rows directly would prove nothing about the system.

The PTT model is documented in the module: a baseline around 250 ms, a stable stretch, one
engineered deviation → repeat → cuff-confirmation sequence, a settled lower level afterwards, and
a recalibration that re-anchors to it. Every number is illustrative and every row is
`synthetic=true`. They are not measurements and must not be cited as evidence of anything.

### `replay` uses only public endpoints

Token, nonce, submit — the same three calls a handset makes, with `X-Session-Nonce` and
`Idempotency-Key`. It resolves `episode_id` and `device_profile_id` through `GET /v1/episodes`,
the timeline and `GET /v1/sessions/{id}` when they are not supplied, and refuses to guess when
more than one episode is visible. Keys beginning with `_` are stripped from the sample files
before posting, because the API forbids unknown fields and the samples carry inline notes.

---

### The seeded rejection rate is conservative on purpose

`DEFAULT_REJECTION_RATE = 0.32`, configurable with `tera-seed-demo --rejection-rate`.

The MVP's ~80% usable target is stated for **controlled seated conditions**. The seeded episode
is not that: it is a 52-year-old self-administering at home, unsupervised, holding a phone
against their sternum with one hand and a fingertip on the lens with the other, twice a day for
four weeks. Carrying the controlled-conditions figure into that setting would be assuming away
the hardest part of the problem, and a demo showing near-perfect acquisition invites exactly the
question we cannot answer.

Reasons are weighted toward the two failure modes that dominate unsupervised capture — the
patient moving (`excessive_motion`, `posture_unstable`) and sensor/lens contact being wrong
(`poor_signal_quality`, `insufficient_beats`) — which together account for ~70% of rejections.

Two consequences worth stating plainly:

- **The rate is engineered, not emergent.** Failures are drawn per attempt as retries clustered
  around a scheduled slot, which is realistic, but the draw is stochastic and on the fixed seed
  lands around 23%. A demo whose headline yield moves with the random seed is not one to stand
  behind, so `_top_up_rejections` makes the shortfall up deterministically and the CLI reports
  both the target and the achieved figure.
- **The reason distribution is therefore not purely `REJECTION_WEIGHTS`.** The top-up also
  guarantees every reason appears at least once, because the clinician summary's per-reason
  breakdown needs something to break down.

None of it is a measured acquisition yield. No acquisition study has been run, and the CLI says
so on the way out (invariant 9).

---

## Bugs found while building

### `fileConfig` in `alembic/env.py` was disabling every application logger

`logging.config.fileConfig` defaults to `disable_existing_loggers=True`. Every `app.*` logger is
created at module import time by `get_logger(__name__)`, so running a migration in the same
process as the API silenced application logging completely. Found by
`test_logs_contain_no_clinical_values` capturing nothing at all. Fixed with
`disable_existing_loggers=False`.

### `logging.LoggerAdapter` was discarding all structured context

`LoggerAdapter.process()` overwrites `kwargs["extra"]` with the adapter's own dict, so every
`extra={...}` passed by a call site was dropped and the "structured logs" contained only a
message. `get_logger` now returns a plain `Logger`.

### Postgres puts the whole failing row in the exception message, before the `[SQL:` marker

The scrubber originally truncated an exception message at SQLAlchemy's `[SQL:` / `[parameters:`
markers. But Postgres prepends its own line first:

```
new row for relation "cuff_reading" violates check constraint "ck_cuff_systolic_above_diastolic"
DETAIL:  Failing row contains (bd2cfbf5-…, 113, 187, 133, manual_entry, …).
[SQL: INSERT INTO cuff_reading (…) VALUES (…)]
[parameters: {'sys': 113, 'dia': 187, 'pulse': 133}]
```

`DETAIL:` carries every column of the offending row as positional values and comes *first*, so
truncating at `[SQL:` left the entire clinical record in place. Found by
`test_database_integrity_error_does_not_leak_bound_parameters`, which provokes a genuine CHECK
violation rather than simulating one — and asserts the raw driver message *does* contain the
values before asserting the scrubbed one does not, so the test fails loudly if the leak vector
ever disappears and the scrubber becomes dead code.

`DETAIL:`, `HINT:` and `CONTEXT:` are now truncation markers alongside the SQLAlchemy ones. The
useful part — the name of the constraint that failed — precedes all of them and survives.

### `CLINICAL_TABLES` and the migration's trigger list had drifted apart

`calibration_source_session` had an append-only trigger in the migration but was missing from
`app.models.CLINICAL_TABLES`, so nothing tested it.
`test_clinical_tables_match_the_migrations_trigger_list` now loads the migration module and
compares the two lists directly.

### The parametrised append-only test was passing vacuously

`test_clinical_tables_reject_update_and_delete` asserted only that a trigger existed in
`pg_trigger`. Row-level `BEFORE UPDATE OR DELETE` triggers do not fire against an empty table,
and the tables were empty. It now runs on a fixture that populates every clinical table, issues
a real `UPDATE` and a real `DELETE` against each, asserts both raise, and asserts the row
count is unchanged — plus a guard that fails if any table is empty when the test runs, so it
cannot silently return to proving nothing.

---

## The submit path is verified against a disposable database, never the demo episode

The ingest path — authenticate, take a nonce, `POST /v1/sessions`, get an estimate back, see the
record in the timeline — had never been observed running end to end. Only its parts had tests.

Verifying it writes a session, and **clinical rows are append-only by trigger**: there is no undo.
Replaying into the demo episode would have left a permanent record a judge would see in the
timeline, and the only way to remove it is `tera-seed-demo --reset`, which truncates everything and
re-seeds with fresh UUIDs — so the demo episode id changes and any bookmarked URL breaks. The
cleanup is worse than the pollution.

So the check runs against a database created for it and dropped afterwards:

```bash
docker exec tera-backend-db-1 psql -U tera -d tera -c "CREATE DATABASE tera_replay_check;"
export TERA_DATABASE_URL="postgresql+psycopg://tera:tera_dev_password@localhost:5434/tera_replay_check"
alembic upgrade head && tera-seed-demo
uvicorn app.main:app --host 127.0.0.1 --port 8001      # a second instance, demo untouched on 8000
tera-replay samples/session_normal.json --base-url http://127.0.0.1:8001 --username ... --password ...
tera-replay samples/session_rejected.json --base-url http://127.0.0.1:8001 --username ... --password ...
# ... then drop the database
```

Pointing a dashboard at `TERA_API_URL=http://127.0.0.1:8001` completes the picture: both replayed
sessions appear at the head of the patient timeline, the accepted one as an outlined "Within your
usual range" row and the rejected one as a dashed "Spot check not usable" row.

Verified 9 August 2026: 201 on both, a trend estimate computed against the calibration in force,
timeline items 82 to 84, and the demo episode confirmed still at 82 afterwards.

**What this does not prove.** Everything above went through the public API from a laptop. The
handset has still never submitted anything — the phone-to-network segment is the one part of the
chain with no evidence behind it, and it needs hardware.

## Deliberate limitations

### Rate limiting is per-process on the ingest endpoints only

**Superseded in part.** The auth endpoints are now Postgres-backed and correct across processes —
see "Postgres-backed rate limiting on the auth endpoints" below.

`FixedWindowRateLimiter` still holds counters in process memory for ingest, summary and nonce, so
with N API workers the effective ceiling there is N times the configured one. That remains
acceptable: on those endpoints the limit protects capacity, and the cost of an over-generous
ceiling is some extra database work. It was *not* acceptable on auth, where the ceiling is the
brute-force defence, which is why that one moved.

### ~~Refresh tokens are not revocable~~ — resolved

**No longer true.** C1 shipped the `refresh_token` table, rotation, revocation and family-level
reuse detection. A leaked token no longer survives to its TTL: logout revokes it, rotation
supersedes it, and replaying a superseded one revokes the whole family. Kept here, struck through,
because the sentence was quoted into a hand-over and someone will come looking for it.

### The device eligibility bands are specified figures, not validated benchmarks

**Updated.** The accelerometer bands are no longer reasoned from first principles: they are the
proposal's own figures (page 7 — 200 Hz minimum, 500 Hz target). The camera, hardware-level and
clock-stability bands are still reasoned from the measurement requirement — the PTT differences
being tracked are milliseconds to tens of milliseconds, so sampling interval and clock stability
have to sit well below that.

Either way, **none of it has been validated against hardware.** Phase 3's profiler
produces the measured numbers, and invariant 9 forbids inventing them in the meantime.
`app/config.py` states plainly which of its defaults are cited and which are design choices.

### No `TODO` comments

Per §7. Everything not built is listed here or in §8's out-of-scope list.

---

## Dependencies

Each with the one-line justification §7 asks for.

| Package | Why |
|---|---|
| `fastapi` | Mandated stack. |
| `uvicorn[standard]` | ASGI server for local runs and the Compose `api` service. |
| `sqlalchemy` | Mandated stack. |
| `alembic` | Mandated stack. |
| `psycopg[binary]` | Postgres driver; psycopg3 is the current generation and ships wheels. |
| `pydantic` | Mandated stack. |
| `pydantic-settings` | Env-var settings binding, so no threshold is ever a literal in code (invariant 10). |
| `pyjwt` | JWT access and refresh tokens (§4.5); no server-side session store needed. |
| `bcrypt` | Password hashing. Used directly rather than through `passlib`, which is one fewer dependency and has been unmaintained since 2020. |
| `python-multipart` | Required by FastAPI to parse the OAuth2 form-encoded token request. |
| `httpx` | `replay` posts through the real HTTP API; also the test client transport. |
| `typer` | Argument parsing for the two mandated CLIs. |
| `pytest`, `pytest-asyncio`, `anyio` | Mandated test runner and the transport its client needs. |

No Redis. No component library. No ORM plugins.

---

## Phase 2 — Dashboard

Scoped slice: palette and design system, plus the clinician episode summary and the patient
timeline. The other three screens in BUILD_SPEC 5.3 are not built yet.

### The three record types are distinguished by form, not by hue

BUILD_SPEC 5.2 asks for them to be unmistakable at a glance without reading labels. Colour alone
cannot do that — it fails in greyscale, in bright sunlight on a phone, and for a reader with a
colour-vision difference — and colour is forbidden from carrying clinical meaning anyway (5.1).
So the distinction is structural:

| Type | Form |
|---|---|
| Cuff reading | Solid `--brand` fill, white text, 3.25rem numerals, unit stated |
| Trend estimate | Outlined, no fill, **no numerals in the value area at all** |
| Rejected session | Dashed border, 0.78 opacity, reason text, retry affordance |

Fill / outline / dash is legible as three different things before a single word is read.

### Contrast was measured, not assumed

BUILD_SPEC 5.4 asks specifically about `--muted` on `--surface`. Measured ratios are in a
comment at the top of `dashboard/app/globals.css`. `--muted` on `--surface` is **5.29:1** —
passes AA at every text size, fails AAA — so it is used as-is for body-sized secondary text and
`--color-ink-800` (7.89:1) is used below 14px. `--color-ink-500` is 2.96:1 and is restricted to
borders and rules; it must never carry text.

### "No numerals at all" is scoped to the value area

BUILD_SPEC 5.2 says a trend estimate shows "no numerals at all". Taken absolutely that would
also forbid the timestamp, which would make the record unreadable. The rule is applied to the
**value area** — the region a patient reads as "the result" — which contains a direction arrow,
a sentence and a three-step signal meter, and not one digit. The timestamp sits in the footer in
small secondary text where it is unmistakably a date.

Two consequences follow:

- `magnitude_sd` is available on the object and is **not rendered** in the patient view. Showing
  it would put a number exactly where a patient expects a blood pressure.
- Confidence is a three-step meter labelled "limited / moderate / strong signal", not a
  percentage. The wording stays about the signal, never about the patient.

The clinician view *does* show `magnitude_sd`, because a clinician needs to know how far outside
baseline a session fell. It is labelled "2.6 SD / of this patient's own baseline" and carries the
estimate badge. BUILD_SPEC 5.4 forbids rendering it as though it were mmHg, not rendering it.

### Warning treatment uses the ink ramp, never a hue

The palette has no red and no green and none may be added. System-state attention — stale cuff
reading, no active calibration, unsynchronised sessions, a rejected session — is carried by a
3px `--ink` left rule on an `--ink-100` field (`.system-flag`), plus dashed borders and reduced
opacity. Nothing in the interface uses colour to say a physiological value is good or bad.

### TypeScript enforces the record-type separation

`TimelineItem` is a discriminated union on `record_type`. Reaching for `systolic_mmhg` on a
`TimelineTrendEstimate` is a compile error rather than a runtime `undefined` rendering as a
blank where a number should be, and the `switch` in the timeline page is exhaustive so a new
record type added to the API fails the build instead of silently not rendering.

### No mock data layer, and a broken page does not look like an empty one

BUILD_SPEC 5 requires running against the seeded backend. There are no fixtures in the project.
When the API is unreachable the page renders `ApiErrorNotice`, which says so explicitly and
states that no records are shown because none could be read — not because none exist. An empty
timeline and a broken timeline must not look the same.

### Dashboard authentication is demo-only

Server components obtain tokens with the password grant using the seeded demo accounts from
`dashboard/.env.local`, cached in module scope until a minute before expiry. Credentials never
reach the browser (`server-only` import guard). A real deployment needs a login flow with a
per-user session and a clinician who authenticates as themselves; this slice is about the record
treatments, and building a login screen would not have tested any of them.

### No charts in this slice

A trend chart is not among the requirements for either screen, and a time series of
`magnitude_sd` is the single easiest way to make an estimate look like a blood-pressure chart.
Deferred until the record treatments have been reviewed.

---

## Phase 3 — Device capability profiler

### The capture layer is a separate package, and this is what it does not do

`packages/tera_capture/` is a Flutter **plugin** — Dart *and* Kotlin — with no dependency on any
UI. The profiler is its first consumer. The patient capture app will be its second.

A DEVIATION from BUILD_SPEC §3's layout, which lists only `profiler/`. The reason: the profiler
is not a throwaway tool, it is the acquisition layer shipped first because it has a deadline.
Welding the camera and sensor code to the profiler's UI would mean writing it twice.

**What the capture package does, and only this:**

| | |
|---|---|
| `configure` / `start` / `stop` | Open and close the accelerometer and the rear camera |
| Accelerometer stream | Timestamped samples, in the sensor's own time base |
| Frame stream | One region-of-interest mean per frame, with its processing time |
| Achieved-rate reporting | Mean rate, interval SD, p99 interval, dropped estimate |
| Device context | Thermal status, battery level and charging state |
| Clock offset | Realtime vs uptime, with a precision flag |

**What it deliberately does NOT do.** This is the boundary the patient app will build on, and
naming it is what stops the profiler quietly growing into the patient app:

- **Buffer retention.** Samples are streamed and dropped. Nothing is kept and nothing is written
  to disk, because invariant 2 says raw sample buffers are never persisted. A consumer that
  needs a window holds it in memory and is responsible for not writing it anywhere.
- **Filtering.** No band-pass, no detrending, no smoothing, no resampling. The streams are what
  the hardware reported.
- **Event detection.** No aortic-valve-opening detection in the accelerometer trace. No foot- or
  peak-detection in the intensity series.
- **Beat pairing.** No association of an SCG event with a PPG event, and therefore no
  transit-time interval. **The package cannot produce a PTT value**, which is the strongest form
  invariant 2 can take at this layer.
- **Quality gate.** No decision about whether a capture is usable. The profiler grades a
  *handset*; the gate grades a *session*. Different question, different code.

If any of those five appears in `packages/tera_capture/`, the boundary has moved and this entry
is a lie. Put them in the consumer.

### Invariant 2 is structural in the Kotlin, not a rule

`CameraCaptureController.consumeFrame` reduces a frame to one `Double` and closes the `Image` in
a `finally` before returning. There is no reference to frame data outside that method, and no
type on the channel that could carry one. A future developer cannot persist a frame from Dart,
because a frame never reaches Dart.

### Every profiler value is a `Measurement`, not a number

BUILD_SPEC 6.2: "Report measured values only. If a measurement fails, say so — never substitute
an estimate or a plausible-looking number."

`Measurement<T>` is either `ok(value)` or `failed(reason)`. There is no default, no nullable
double that renders as `0.0`, and `requireValue` throws rather than yielding a zero. The
markdown row prints `not measured` in the cell.

The failure this guards against is not a crash. It is a run where the camera never opened and
the report shows `0.0 fps` — a figure a reader takes at face value and pastes into the
proposal's device table. `profiler/test/measurement_honesty_test.dart` asserts that a wholly
failed run produces a markdown row containing **no digit at all**.

Two consequences worth stating:

- `RateStatistics.fromTimestamps` returns null for fewer than three samples, and null for
  non-monotonic timestamps. Skipping a bad sample would hide a stream that is not what it
  claims to be.
- The upload to `POST /v1/device-profiles` **refuses** when any field the API requires could not
  be measured, and says which. The API has no way to record "not measured" for those fields, and
  a placeholder would put an invented number into a clinical record (invariant 9).

### The clock basis is measured, not believed

`SENSOR_INFO_TIMESTAMP_SOURCE` is a **declaration**, and `SensorEvent.timestamp` is *documented*
as `elapsedRealtimeNanos`. Neither is universally honoured.

The failure this guards against is invisible by every other measure. A handset that declares
`REALTIME` but timestamps frames in the uptime base has a correct frame rate, correct jitter,
correct intervals — and every cross-stream alignment computed from it is wrong by however long
the device has spent asleep since boot. Hours, typically. Nothing in the rest of the profile
would show it.

So every sample from both streams carries `elapsedRealtimeNanos` and `uptimeNanos`, read back to
back at delivery, before any other work in the callback. A timestamp must sit a plausible
pipeline latency behind whichever clock it is expressed in (−5 ms to +500 ms) and implausibly
far behind the other. `ClockBasisVerification` reports `realtime`, `uptime`, `indeterminate` or
`neither`.

- **`indeterminate`** when the two clocks differ by less than 10 ms — a freshly booted device
  that has not slept. Not a failure, just not an answer, and the verdict says to leave the
  handset idle and re-run.
- **`neither`** is a real finding: the timestamps are in some third base and nothing can be
  aligned against them until it is identified.

The accelerometer is checked the same way even though it declares nothing, because
`CrossStreamClockCheck` — do the two streams share a base — is the question that actually
decides whether a transit time is measurable at all. It has its own column in the markdown
table, since a handset that fails it cannot produce a PTT whatever its frame rate says.

Cost: two vDSO clock reads and two extra longs per sample, on both streams equally, so the
cold/warm comparison is unaffected. The analysis reuses the samples the two 60 s runs already
produced rather than adding a stage.

### Smoke mode is a different type, not a flag

`runSmoke()` returns a `SmokeReport`, not a `ProfileResult`. There is no conversion between them
and no `smoke: true` field on a shared type.

Five seconds of camera is not a sustained-rate measurement and never becomes one. A boolean flag
would rely on every export path remembering to check it; a separate type means the code that
builds the device eligibility table is structurally unable to accept smoke output. The report
shows observed numbers — that is what makes the debugging loop fast — under a header saying they
are not measurement data.

### `confidence_ceiling` is bounded, unlike every other threshold

`CONFIDENCE_CEILING_LIMIT = 0.95`, enforced at config validation, env-var path included.

Every other clinical threshold is tunable because a clinic may legitimately disagree with the
default. This one is not. Raising it toward 1.0 would not change what the number *is* — a blunt
ordering of sessions by how much usable signal they produced — but it would change what it
*looks like*, and a reader who sees 0.99 reads certainty into a heuristic that cannot support
it. That is invariant 6 by the back door: not a diagnosis, but a claim of accuracy the method
does not have. Lowering it is always allowed; there is no floor on modesty.

A model validator also rejects a floor above the ceiling, weights that do not sum to 1.0, and an
inverted SNR range. Each would still produce numbers that look like confidences, which is
exactly why they fail at startup rather than degrading quietly.

### The profiler does not compute a verdict

It measures; the backend grades. The eligibility bands live in `backend/app/config.py`, so
changing a threshold does not mean reflashing eight handsets. The upload response shows the
backend's verdict, labelled as the backend's.

### The warm camera run has no cool-down

BUILD_SPEC 6.4 asks for the repeat "immediately". `ProfileRunner` starts the second 60 s run
directly after the first with no pause and no teardown delay beyond closing the session. Any
gap would let the device recover and hide the throttling the run exists to find.

### Rates are measured, never requested

`SENSOR_DELAY_FASTEST` with `maxReportLatencyUs = 0` (batching off, or delivery arrives in
bursts and the timing means nothing), and the fastest advertised AE target FPS range for the
camera. What is *reported* is computed from `SensorEvent.timestamp` and `Image.timestamp` —
hardware timestamps, never the time a callback happened to run, which in a garbage-collected
runtime measures the runtime.

### `HIGH_SAMPLING_RATE_SENSORS` is declared in the plugin, not the app

So the patient app inherits it by depending on the plugin rather than by remembering. Verified
present in the profiler's merged release manifest. Below API 31 the permission does not exist
and no cap applies, so it is reported as granted — which is the truthful answer to the question
the field actually asks ("are rates above 200 Hz available to this app").

### `uptimeNanos` needs API 31; below it the offset is millisecond-resolved

`ClockOffsetSample.uptimeHasNanosecondPrecision` is false on those devices and the UI says so. A
millisecond is the same order as the effect being measured, so hiding the limitation would be
worse than the limitation.

### No `path_provider`, no `share_plus`

Export writes to the app-specific external files directory, resolved directly. One fewer
dependency to install on eight borrowed handsets in a hurry. `http` is the only non-Flutter
dependency, and only for the optional upload.

---

## Auth and tenant isolation

Covers commits C1–C3. Two source documents govern this repo: **`docs/proposal.pdf`** is the
authority for product scope, clinical constraints, safety requirements, device thresholds and
success metrics; **`BUILD_SPEC.md`** is the authority for API surface, payload contracts, schema
and constraint sketches. Where a requirement comes from neither, it is labelled as such below
rather than dressed up as one of them.

### Password grant is a project requirement, not a proposal requirement

The proposal does not mention OAuth at all. Its security controls (Table B1) are "consent-based
access, patient and clinician role separation, audit trails, and data minimisation", plus
"encryption in transit and at rest". BUILD_SPEC 4.5 says "OAuth2 with short-lived JWT access
tokens plus refresh" without naming a grant.

An earlier instruction asked for authorisation-code flow on the basis that the proposal
specified it. It does not — that requirement came from an appendix table in an older draft that
was cut when the proposal was condensed. It is therefore **a requirement from the project
owner**, not from either source document. Recorded that way deliberately: calling it "aligning
with the proposal" would be false, and the next person would go looking for a section that no
longer exists.

**Decision: keep the password grant.** For a first-party client with no third-party identity
provider, authorisation-code adds an authorization server, redirect-URI registration and a
custom-tab flow on Android for no gain in security — the client already handles the password.

**Migration path** if SSO is ever required: add `GET /v1/auth/authorize`, make
`POST /v1/auth/token` accept `grant_type=authorization_code` with PKCE alongside the existing
grant, keep the refresh-token machinery from C1 unchanged (it is grant-agnostic), then retire
the password grant once every client has moved. Nothing in the current design blocks that.

### Registration is admin-only, and that is an implementation detail

The proposal describes enrolment as clinic-initiated: a patient is enrolled into a monitoring
episode when their treatment is adjusted, by the clinic. There is no self-service sign-up in the
product as described, so `POST /v1/auth/register` requires the `admin` role.

The `admin` role itself comes from BUILD_SPEC 4.5, not from the proposal, which names only
patient and clinician. Both are implementation details rather than proposal requirements.

A public sign-up form would let anyone create an account holding clinical data with no clinic
behind it, which is a different product from the one described.

### 404 for cross-tenant, 403 for lacking authority

Applied consistently and locked by `tests/test_tenant_isolation.py`:

- **404** when the caller is not entitled to know the resource exists — anything belonging to
  another patient. A 403 confirms the id names a real row, which is a disclosure about someone
  else's care even when no field is returned. Someone holding a list of candidate ids could
  separate the real from the invented.
- **403** when the resource is not secret but the caller lacks authority — a patient hitting the
  admin-only register endpoint, or asking for the clinician summary of their *own* episode. They
  already know it exists; refusing tells them nothing new, and 404 would be a lie that makes the
  client harder to debug.

`assert_patient_scope` returns **403 and is right to**, even though it guards cross-tenant
writes: it compares against the `patient_id` in the caller's own token *before any database
lookup*, so the answer is identical for a patient record that exists and one that does not. The
distinction that matters is not "cross-tenant versus authority" but **whether the response can
distinguish an existing row from a missing one**.

Two real leaks were found and fixed while applying this. `GET /v1/calibrations/{id}` and
`GET /v1/device-profiles/{id}` loaded the row first and checked ownership second, so an invented
id returned 404 while another patient's real id returned 403 — a probe oracle. Both now use
`assert_owns_or_404`, and the tests assert the two responses are byte-identical rather than
merely both being refusals.

### The clinician summary is closed to patients

Proposal, page 4: the exception summary is a "role-protected **clinician** web view". It was
previously readable by the patient who owned the episode.

This is not about withholding data — every record in it is the patient's own and all of it is on
their timeline. The summary is written for a clinician, in clinical shorthand, prioritising
exceptions; it is not the interface a patient should read their own care through. Returns 403
per the rule above, and records `clinician_access_denied` in the audit log.

### `refresh_token` is deliberately not append-only

Every clinical table carries a trigger rejecting `UPDATE` and `DELETE` (invariant 5).
`refresh_token` does not, for two reasons:

1. **Revocation is an update.** Marking a token revoked or superseded is the entire purpose of
   the table. Under the trigger it would be impossible, and logout would have to be implemented
   by inserting tombstones and reading the latest — the same mutation with more moving parts and
   a worse failure mode.
2. **It holds no clinical content.** A jti, a user id, timestamps, a revocation reason. Session
   bookkeeping, not a record of care, and invariant 5 exists to protect the latter.

The history is preserved where it matters: `refresh_token` is mutable current state, and
`audit_log` — which *is* append-only — is the immutable record of what happened to it.

---

## Postgres-backed rate limiting on the auth endpoints (C5, shipped)

The design below was recorded before implementation and is now shipped as described, in
`app/security/authlimit.py`, `app/models/ratelimit.py` and migration `0006_rate_limit_counter`.
Four things the design did not anticipate are recorded at the end.

### Why the current limiter is not sufficient

`FixedWindowRateLimiter` holds counters in process memory. With N API workers the effective
ceiling is N times the configured one. That is tolerable on the ingest endpoints; it is not on
the auth endpoints, where the ceiling *is* the brute-force defence.

### Design

One table, following the `session_nonce` precedent — a second datastore is not justified when
Postgres is already there:

```
rate_limit_counter
  id, bucket, subject_key, window_start, count
  UNIQUE (bucket, subject_key, window_start)
```

Increment atomically across processes in a single statement, so two workers cannot both read a
count below the limit and both allow the request:

```sql
INSERT INTO rate_limit_counter (bucket, subject_key, window_start, count)
VALUES (:bucket, :key, :window, 1)
ON CONFLICT (bucket, subject_key, window_start)
DO UPDATE SET count = rate_limit_counter.count + 1
RETURNING count
```

### Keys per endpoint

"Per-token and per-patient" cannot apply at login: there is no token yet, and which patient is
meant is not yet known. The meaningful keys differ by endpoint.

| Endpoint | Keys |
|---|---|
| `POST /v1/auth/token` | attempted username, and client address |
| `POST /v1/auth/refresh` | **refresh-token family**, and client address |
| `register`, `logout`, `me` | per-token, and per-patient where one is in scope |

**Family keying on refresh is the precise defence.** `family_id` already represents exactly one
login, and reuse detection (C1) already operates at that granularity, so the unit is available
and meaningful. Client address alone fails behind NAT and CGNAT, where an attacker shares an
address with legitimate users and a per-address limit either lets the attack through or locks
out the bystanders. Coarse per-address plus precise per-family gives both.

**On repeated breach of the family limit, revoke the family.** A client hammering refresh with
one family's tokens is either broken or hostile; in both cases ending that login is the correct
response, and `revoke_family` already exists.

### Thresholds

All from `SecuritySettings` via pydantic-settings, never literals — the existing
`ingest_rate_limit_*` fields are the pattern to follow. Shipped values, each with its reasoning in
`config.py`: 10 login attempts per username per 15 minutes, 60 per address per 15 minutes, 20
refreshes per family per hour, 120 per address per hour, family revoked at 20 past the limit.

### Four things the design did not anticipate

**1. The counter has to commit separately.** A failed login rolls its transaction back — that is
how the audit record and the failure response coexist — and an increment inside that transaction
rolls back with it. A limiter whose increments vanish along with the failures they were counting
counts nothing at all. `authlimit.check` commits its own increment.
`test_counters_survive_the_failed_login_rollback` is the guard.

**2. The limit must apply before the password is verified, and must hold even when the password
is right.** Checking after verification still performs the expensive hash comparison on every
attempt, which is most of what an attacker is trying to make the server do. And if a correct
password bypassed the counter, an attacker would be throttled right up until the attempt that
succeeded — the only one that matters. Both are tested.

**3. Subject keys are hashed before storage.** Not in the original design. An attempted username
is credential-adjacent — a table of failed logins is a list of usernames worth trying — and a
client address is personal data. Neither belongs in a table whose only job is to answer "how
many". This is not protecting a secret; the input space is small enough that a targeted guess
could be confirmed. It is making sure a dump of a counting table is not also a target list.

**4. `X-Forwarded-For` is deliberately not trusted.** Behind a proxy it is authoritative; in front
of one it is attacker-controlled, and honouring it unconditionally would let any caller reset
their own limit by inventing an address. Deploying behind a real proxy means configuring proxy
headers middleware, not reading the header at the endpoint.

Also worth knowing: the counter increments **whether or not the request is allowed**, unlike the
in-memory limiter. That is what makes a caller hammering a locked bucket visible rather than
invisible, and it is what lets the refresh endpoint distinguish "over the limit once" from "over
the limit repeatedly" — the signal that justifies revoking a family.

`rate_limit_counter` had to be added to `ALL_TABLES` in `conftest.py`: counters now outlive a
test, and one test's failed logins would otherwise exhaust another's allowance.

---

## Device eligibility rebanded to the proposal's figures

The bands were 200 Hz qualified / 100 Hz provisional. The proposal (page 7) specifies a **minimum
of 200 Hz and a target of 500 Hz**, with non-compliant handsets excluded at onboarding rather than
permitted to produce estimates whose error exceeds the signal. The bands are now:

| measured rate | verdict |
|---|---|
| below 200 Hz | `not_qualified` |
| 200–500 Hz | `provisional` |
| 500 Hz and above | `qualified` |

The old banding treated 200 Hz as the *target*. It is the floor — the point below which the timing
error is larger than the effect being measured (the proposal's own figure: 10.6 ms jitter at
100 Hz against a signal carried in 10–50 ms shifts). A handset sitting just above the floor is not
"qualified"; it is usable with a stated caveat. This also aligns the backend with the patient app,
which was already gating on 200/500.

### The consequence that shapes everything else

Android caps sensor delivery at 200 Hz unless the app holds `HIGH_SAMPLING_RATE_SENSORS`
(Android 12+). **So the great majority of real handsets land in the provisional band.** Provisional
is the normal case, not the exception, and two things follow.

**It gates nothing, and a test now says so.** `qualified_status` is computed, stored and rendered;
it is never consulted before accepting a session or producing an estimate.
`test_provisional_status_gates_nothing` submits an identical session against a provisional and a
qualified profile and requires the same status code — and asserts the qualified case is a 201
first, so the test cannot pass by both being rejected. If someone later makes the status a gate,
the eligibility rule would live in two places, one documented and one not, and patients on the most
common class of Android handset would silently stop getting estimates.

**The finding explains itself inline.** A bare `PROVISIONAL` invites the reader to supply their own
meaning, and the meaning they supply is worse than the truth. The explanation now states, in the
finding itself, that the handset is cleared for use, that nothing is restricted, that this is the
usual result rather than a fault, and that the real consequence is more repeat spot checks — never
a less trustworthy estimate. `test_provisional_explains_itself_without_a_lookup` pins that wording.

### Seeded device profiles say what they are

A seeded device profile is the one synthetic record that reads as a hardware benchmark: `204.8 Hz`
looks like something somebody measured on a bench. The generic badge — "not a real measurement" —
carries the wrong reassurance, because it is about measurements from a *person*. Invariant 9 names
device benchmarks specifically ("never invent device benchmark results"), so these carry their own
notice: **"SYNTHETIC SEED DATA — ILLUSTRATIVE OF UI STATES, NOT MEASURED PERFORMANCE"**.

The demo profile is deliberately left at 204.8 Hz, in the provisional band, so the demo shows the
state a reviewer is most likely to encounter rather than an idealised one.

---

## Environment notes

The Compose Postgres publishes on host port **5434**, not 5432 or 5433 — both were already taken
by other Postgres instances on the development machine. Change it in `docker-compose.yml` and
`backend/.env` together if that does not suit.

## Compose takes the JWT secret from `backend/.env` via `env_file`, not `${VAR}`

The API container signed tokens with a 42-byte string hardcoded in `docker-compose.yml`, while
every host process — pytest, the CLIs, a local uvicorn — used the 64-byte value in `backend/.env`.
A token minted by one was rejected by the other, and nothing said so: the failure surfaced as a 401
that looks identical to a wrong password.

`${TERA_JWT_SECRET}` substitution cannot fix it. Compose resolves `${VAR}` from a `.env` **beside
the compose file**, which is `tera-backend/.env` — a file that does not exist and must not, because
the secret already lives one directory down and duplicating it recreates the same divergence in a
new place.

`env_file: ./backend/.env` reaches the real file. The `environment:` block is kept and now carries
only non-secrets, which is what makes this safe: `backend/.env` points `TERA_DATABASE_URL` at
`localhost:5434`, correct from the host and wrong from inside the compose network, and
`environment:` overrides `env_file:` so the container keeps `db:5432`.

Verified 9 August: a token minted by the containerised API verifies against the 64-byte
`backend/.env` secret, and the old 42-byte compose string is rejected.

## B2C: a patient no longer needs a clinic

The product is a standalone consumer app. Nobody enrols anyone, and there is no clinic behind an
account.

**`patient.clinic_id` is now nullable** (0007). That column, not `reviewing_clinician_id`, was the
real coupling: it was NOT NULL, so a clinic identifier was a precondition for a patient record
existing at all. `monitoring_episode.reviewing_clinician_id` was already nullable in 0001 and
needed no change — it was introduced as optional precisely so an episode could exist before a
clinician was assigned.

**Null, never a placeholder.** `clinic_id` is left null on the patient and the user, and
`reviewing_clinician_id` null on the episode. A string like `"SELF"` would be a clinic affiliation
that does not exist, written into a clinical record.

`ClinicianSummaryOut.clinic_id` and `EpisodeListItem.clinic_id` were relaxed to `str | None` to
match. The full suite caught this immediately, which is the reason the response schemas are
explicit rather than inferred.

The downgrade **refuses to run** when any patient has a null `clinic_id`. Restoring NOT NULL would
need a value for every self-registered patient and none would be true.

### `/v1/auth/register-patient`

`/register` stays admin-only. This is a separate route rather than a relaxation of that one,
because the two have different threat models and merging them would put a role parameter on a
public endpoint.

- **No `role` field at all.** It mints patients and nothing else. A role parameter on an
  unauthenticated route is a privilege-escalation surface, and defaulting it is one typo away from
  a self-service admin account. A request carrying `role` is rejected with 422 rather than ignored.
- **Account, patient record and first episode in one transaction.** A patient account without a
  patient record violates the database CHECK, and a patient without an episode has nowhere to
  record anything, so a partial success is worse than a failure. A duplicate subject creates no
  orphan patient row — asserted.
- **Rate limited per address before anything is written.** This is the only unauthenticated route
  that *writes*, so it is the only one where an attacker gets rows rather than rejections. Five per
  hour per address: a real person signs up once.
- **The pseudonym is random, not derived from the subject.** BUILD_SPEC 4.1 has nowhere to put a
  name, and deriving it from an email address would put one there sideways. Asserted.
- **`protocol_params` is empty**, so every threshold falls back to `app.config`. Inventing
  per-episode values would present an engineering default as a clinical decision nobody made.
- Tokens are returned, so signing up does not immediately require a second round trip to a login
  form the user has just filled in.

## `patient_context`: the intake reaches the server, append-only

The intake was handset-only, so it vanished on uninstall and the server could not see a
contraindication it is expected to respect.

**Append-only, latest-wins on read** (invariant 5), with the shared `tera_append_only_guard`
trigger. A changed answer inserts a row and the previous one stays: what the patient said in June
is a fact about June, and a session captured then should be read against the context in force at
the time. Same reasoning as `calibration`.

**The patient comes from the token.** `PatientContextCreate` has no `patient_id`, so filing context
against somebody else's record is not expressible. A body carrying one is rejected — `TeraModel`
forbids unknown fields.

**`last_clinic_*` is three columns, not JSONB**, so a CHECK can hold them together: all three
present or all three absent, systolic above diastolic. A systolic with no date is not a reading.
The request schema says the same thing, so a client gets 422 rather than a constraint violation.

**The medication list is bounded at 32.** Invariant 2 in spirit — an unbounded JSONB column on an
ingest route is where a series ends up.

`pregnant` is a real Postgres enum, three-valued, matching the handset. The `audit_action` value
`patient_context_recorded` is added by `ALTER TYPE ... ADD VALUE`, and the downgrade leaves it: an
enum value cannot be dropped without rewriting the type, as in 0003.

**The deny-list grew with the table.** `pregnant`, `pregnancy`, `arrhythmia`, `known_arrhythmia`,
`regimen` and `medications` are now denied field names. The new route logs an id and a count and
nothing else, but the deny-list is what makes that hold for the next person too.

### The trigger-list test now asks the database

`test_clinical_tables_match_the_migrations_trigger_list` parsed `0001`'s `APPEND_ONLY_TABLES`.
`patient_context` arrives in `0008`, so that check would have failed for a table that is correctly
configured — and pinning it to one file is how the two lists drifted apart the first time. It now
queries `pg_trigger` for `trg_%_append_only` and compares against `CLINICAL_TABLES`, which is true
of the database rather than of one migration.

## Mobile: local first, server second

`PatientContextSubmitter` posts the intake after the local write, and **never throws, never blocks,
and never gates**. The handset copy is what `ContextIntakeSafety` reads, so a contraindication
holds on a dead network; a patient who reported one is blocked whether or not the server heard.

The wire shape is flat — `last_clinic_systolic_mmhg` and friends — rather than the nested
`last_clinic_bp` the local JSON uses, matching the columns. One mapping, in one place, asserted by
a test.

A failed upload is stated on the screen rather than apologised for: saved here, not yet in your
account, save again when you are back online.

## The pregnancy contraindication is enforced on the server too

The handset refuses a spot check when pregnancy is reported, in pure Dart so it survives a dead
network. It is also **only a client**. An older build, a replayed request, a second client, or
anyone holding a token reaches the API directly, and the server would happily have produced a trend
estimate for a patient the method was never validated on. A safety rule that exists in one of two
halves is a safety rule that exists in neither.

`app/services/contraindication.py` reads the latest `patient_context` row and refuses in three
places:

- `POST /v1/sessions` — **403, before anything about the capture is written.**
- `POST /v1/calibrations` — **403.** A baseline exists only so estimates can be computed against
  it, so this is the same refusal one step earlier.
- `GET /v1/sessions/{id}` — the trend block is withheld and `trend_withheld` carries the reason.
  The session record stays visible: withholding a patient's own history from them is not what this
  gate is for.

**403, not a rejected session.** Invariant 3 keeps sessions the system *processed and found
unusable* — a fact about the capture, which the clinician summary reports. This is not that: the
refusal is a property of the patient, nothing about the signal was examined, and the shape matches
the existing 422 for a malformed payload and 404 for an unknown device profile, both of which
already refuse without storing. Recording it as a rejected session would also write a row stating a
pregnancy into an append-only table on every attempt.

**Estimates recorded before pregnancy was reported are not deleted** (invariant 5). They are not
served either.

**The gate matches the handset exactly, including its weak edges.** Only `yes` blocks.
`prefer_not_to_say` does not, because blocking a declined answer makes declining functionally
identical to saying yes. A patient who never filled the form in is not blocked, because the intake
is not a precondition for using the app. Both are deliberate, both are asserted, and both are the
places this gate does not protect anyone.

`patient_context` is append-only, so the gate reads the most recent row — a correction takes effect
on the next request, in both directions. Asserted.

## The rhythm model loads lazily, off by default, and its artefact is not in git

The ML team shipped a 52 MB scikit-learn Random Forest and a 37 MB `model_trees.json`. The loader
follows the team's previous backend (`JantungSinyal-Backend/ml_service.py`) — prioritised path
resolution with an env override, defensive handling of both bundle schemas — and differs from it in
three places, each of which is a failure that backend has.

**Lazy, not at import.** `ml_service.py` runs `_load_model()` at module scope. A missing artefact
is therefore an `ImportError` that takes down the application and every test that imports near it.
Here the read happens on first use behind a double-checked lock, and every failure is a *state*:
`disabled`, `not_found`, `runtime_missing`, `load_failed`. None of them raises.

**Off by default.** `TERA_RHYTHM_ENABLED` must be set. This is the handoff's own recommendation,
not caution on our part: the model powers exactly one field, nothing else depends on it, and "a
missing flag costs nothing. A false 'irregular rhythm' on a healthy volunteer in front of a judge
costs a lot." Disabled does not even look for a path.

**`joblib` and `scikit-learn` are imported inside the function**, not at module top. Neither is a
dependency of this backend, and the suite has to keep running on a machine with no scientific
stack. An optional model must not become a mandatory import.

**`op_threshold` is read from the bundle and never assumed.** The adult bundle ships about 0.10;
the fallback is 0.5. Substituting one for the other silently would change the model's behaviour
completely, so a bundle without an operating point logs at WARNING and sets
`op_threshold_is_fallback`, which a caller can see.

**The feature count is checked against the bundle.** Wrong feature order gives a wrong answer with
no error at all — the handoff calls the order "WAJIB sama persis" — and a count mismatch is the
part of that this side can actually detect. It fails closed.

**A load failure logs the exception type, never its message.** A pickle error can carry file
contents, and the deny-list does not help if the string is interpolated into a `detail`. Asserted.

**The artefacts are git-ignored.** 90 MB of binaries in a repo is not version control, and a
checked-in pickle rots against the scikit-learn version that wrote it. `ml/MODEL_HANDOFF.md` and
`ml/export_model.py` are tracked; the `.joblib`, `.json` and `.ipynb` are not. Point
`TERA_RHYTHM_PATH` at a local copy, or drop one in `backend/ml_models/`.

## PHR profile, session context, and the rule engine (PM spec 24, 28, 30)

**Two tables, two disciplines, and the difference is the point.**

`phr_profile` is **mutable** and deliberately *not* a clinical record in invariant 5's sense. It
describes a person as they are now — a weight, a hypertension status — so a corrected height is a
correction, not a new fact about a different moment. One row per patient, no append-only trigger.

`session_context` **is** append-only, with the shared trigger. It records what the patient reported
around one measurement. Editing it later would rewrite what was true then. The spec's
`PATCH .../context` is therefore an insert-and-supersede, and reads take the latest row.

The pregnancy and rhythm answers stay in `patient_context` rather than moving into the profile:
those are moment-in-time facts the contraindication gate reads, and they must never be rewritten.

### POST, not PATCH

The spec writes both as `PATCH`, and section 30 opens with "Route names are examples."
`test_clinical_rows_have_no_update_or_delete_route` walks the OpenAPI schema and refuses **any**
PUT, PATCH or DELETE anywhere in the API — deliberately blunt, because the verb is what a client
sees and a mutable-looking route on a clinical resource is an invitation. The verb is not
load-bearing in the spec; the invariant is load-bearing here. For the context route POST is also
the more honest verb: it inserts.

**An absent field means unchanged, not cleared.** Otherwise a screen collecting half the profile
erases the other half every time it saves.

**Condition and symptom codes are closed lists**, so a typo is a 422 rather than a row nobody can
query later. `chest_pain` is refused by the symptom list on purpose: red flags terminate a session
before capture, offline, and one arriving at CTX-01 would be arriving too late to act on.

### The rule engine

Section 24 as deterministic code, in `services/insight.py`. Pure — same features in, same verdict
out, asserted — and it does no IO: the caller assembles the features.

**Decision and wording are separate**, which is why the engine returns codes and `language.py`
holds the sentences. The spec separates `deterministicRuleEngine` from `languageLayer` for the same
reason: the two get reviewed by different people.

**Comparability is checked before the trend is interpreted**, in both the single-change and
persistent rows. A change measured on someone who was not at rest is not evidence of a change in
them, and the matrix says so twice.

**Context never changes the verdict.** A missed dose, poor sleep and higher stress produce context
codes and leave the action alone — section 24 says "no dose change advice" in its own row, and a
test asserts no action wording is a dose instruction.

**The insight is computed on read and stored nowhere.** It is a function of rows that already
exist, so it cannot drift from them and there is no second copy to keep in step. The
contraindication gate covers it too: an estimate withheld on the session detail must not reappear
wrapped in an insight.

### The deny-list cannot tell a claim from its negation

"This is not a diagnosis" trips `\bdiagnos\w*`. The wording became "It does not identify or rule
out any condition" instead. Keeping the deny-list blunt is the right trade — the cost is a reworded
sentence, and the alternative is a deny-list with exceptions in it, which is how exceptions start.

## `check_session`: a session that exists before a measurement does

`measurement_session` is a *sensor capture*. It requires per-beat intervals, a device profile and a
quality block, so it cannot exist before capture and never exists at all for a BP-only check. PRE-01
therefore had nowhere to go, and CTX-01 fell back to an episode-scoped event.

`check_session` (migration 0010) is the spec's own model from section 28: opened at the start of the
flow in **both** modes, carrying a mode and a status that walk the section 31 machine. A sensor
capture links back to it through a nullable `measurement_session.check_session_id` — nullable
because every session submitted before 0010 has none, and inventing a link would be fabricating one.

`session_context` was re-pointed from `measurement_session` to `check_session`. It was created in
0009 the same day and holds no rows; the migration **checks that and refuses** rather than dropping
clinical rows if it is wrong.

**The contraindication gate moved to the door.** `POST /v1/check-sessions` refuses with 403, so a
patient who cannot get a trend is not walked through five screens to be refused at the end.

### PRE-01 is persisted, and `is_ready` is derived

`precondition` is append-only: it describes the patient's state before one measurement, and
rewriting it later would rewrite what was true then.

`is_ready` is computed on the server from the five answers and **not accepted from the client** — a
request carrying it is a 422. Otherwise a client could declare itself ready while reporting a
confounder, and the summary would disagree with what it summarises.

The rule engine now reads it instead of hardcoding `precondition_standard = True`, so section 24's
"non-standard precondition" rows finally fire. **Absent is treated as standard**: those rows are
about a *reported* confounder, and inventing one would refuse a check nobody said anything wrong
about.

### A pre-existing flaky invariant test

`test_no_audit_entry_carries_clinical_content` scanned `actor|target|action` for the strings "191",
"117" and "143". `target` is a hex UUID and all three are valid hex, so it failed on a coincidence
roughly one run in fifty — which is how it surfaced here, unrelated to this change.

`target` is now checked *structurally* — it parses as a UUID, so it cannot carry a clinical value —
and excluded from the substring scan. A flaky invariant test is worse than no invariant test:
people learn to re-run it.

## Section 28 and 30 completed: the gaps, and what was already there

The brief was "build the backend foundation: sections 28 and 30". Most of it existed. Recording
what was genuinely missing, so the next person does not re-audit the same ground.

**Already built, before this change.** `users` (`app_user` + `patient`), `devices`
(`device_profile`), `phr_profiles`, `medications`, `check_sessions`, `preconditions`,
`session_context`, `trend_results` (`trend_estimate`), and the endpoints for auth, profile,
check-session creation, preconditions, context and insight. `health_conditions`,
`lifestyle_profiles` and `family_history` are folded into `phr_profile` as columns rather than
tables — a deviation argued in the 0011 entry, taken because a table per single-valued answer buys
per-field timestamps nobody collects.

**Genuinely missing, now built.**

- `bp_reference` — the last table in section 28 with no home. Which cuff reading is a patient's
  baseline lived only in `AppFlowState.reference` on the handset, so it did not survive a
  reinstall and the server could not read it.
- `/v1/bp-reference` × 3, `/v1/medications` × 4, `/v1/conditions` × 2, `/v1/profile/completion`,
  `/v1/device/eligibility`, `/v1/device/current`, `/v1/history` × 2, and the three check-session
  transitions `/capture`, `/process`, `/complete`.

**`insights` is deliberately still not a table.** Section 28 models it as stored. The insight is a
pure function of rows that already exist and `GET .../insight` computes it on read, so persisting
one would create a second copy free to drift from the facts it summarises. `sensor_measurements`
and `signal_quality` likewise remain `measurement_session` columns rather than separate tables.

### The BP reference is a pointer, not a copy

`bp_reference` names a `cuff_reading`; it does not restate the numbers. Copying systolic and
diastolic into it would put mmHg in a second table and create a way for the two to disagree —
invariant 1 says there is exactly one table holding pressure.

Supersession mirrors `calibration` exactly: a partial unique index allows one `active` row per
patient, a one-way trigger permits retiring an active reference and forbids everything else, and
replacing a reference inserts. `bp_reference` is therefore **not** in `CLINICAL_TABLES` — like
`calibration`, its supersession columns are the one sanctioned mutation and the append-only
trigger would forbid them. Same argument as CLAUDE.md section 5.

`GET /bp-reference/status` biases toward asking (invariant 7): no reference, an unreadable
reading, an unresolvable date all answer `needs_refresh: true`. A false request costs one cuff
measurement; a false "you are fine" is read against a baseline that no longer describes the
patient.

### DELETE /medications/{id} is a status transition

Section 30 lists a DELETE. Section 28 gives `medications` a `status` column, which is the spec
answering its own question: a medication somebody stopped is not a row that never existed, and
what a patient was taking when a reading was recorded is part of reading that record later.
`POST /medications/{id}/stop` sets the status; nothing is deleted. Invariant 5 says the same thing
about clinical history generally, and the OpenAPI-walking test refuses the verb outright.

Every mutating route added here is POST for that reason — `PATCH /profile`, `PUT /conditions` and
`PATCH /medications/{id}` in section 30 are all POST here. Section 30 opens with "Route names are
examples"; the invariant is the part that is load-bearing.

### A medication change forces a reference refresh

PROF-04 requires it and until now nothing set the flag. Add, correct or stop a medication and
`monitoring_episode.force_reference_refresh` goes true for that patient; activating a new
reference clears it. The baseline was established under one medication regime, and reading a trend
against it afterwards compares two different states of the same person.

### The check-session state machine is one table, not four handlers

Section 31's diagram is written down once, as `_ALLOWED_FROM` in `phr.py`, and every transition
route consults it. Transitions are **idempotent**: asking for the state a session is already in
succeeds and changes nothing, because a handset retrying after a dropped response is the ordinary
case and a 409 there would strand a patient mid-flow with no way forward. Illegal transitions and
transitions out of a terminal state are 409 with the reason in words.

`POST .../capture` carries **no signal** (invariant 2). It reports which of section 17's three
gate states the handset reached; derived per-beat intervals still travel by `POST /v1/sessions`,
which stays the only route that accepts them. A `bp_only` session refuses the route outright
rather than recording a capture that never happened.

### Two device vocabularies, collapsed in exactly one place

The profiler grades `qualified` / `provisional` / `not_qualified`; the app branches two ways.
`/v1/device/eligibility` collapses them, and `provisional` maps to **eligible** — the proposal's
minimum band is the minimum at which a capture is meaningful, and refusing it would put a working
handset on the BP-only path. The three-way verdict is returned alongside so nothing is lost.

Unlike `POST /v1/device-profiles`, this route takes no `patient_id`: it is called by the patient's
own handset and the patient comes from the token. A body that could name a patient would let one
handset write a hardware verdict onto somebody else's account.

### History is one list, not four

`GET /v1/history` returns typed entries in one reverse-chronological list, because HIST-01 renders
one column and four parallel arrays leave the interleaving — and therefore the ordering — to each
client. The mmHg fields exist only on `cuff_reading` entries: a trend entry has no field that
could carry a pressure value, so invariant 1 holds structurally rather than by discipline.
Rejected sessions appear (invariant 3, and section 26.3 asks for them by name).

It is patient-scoped rather than episode-scoped, unlike `/v1/episodes/{id}/timeline`, which stays
as the clinician-facing view of one episode.

### `/api/v1` is mounted as an alias

Section 30 writes `/api/v1/...`; this API has served `/v1/...` since 0001 and the patient app is
built against it. The router is mounted twice, with the `/api` copy `include_in_schema=False` — one
operation appearing twice would double `docs/api.md` and hand the schema-walking invariant tests
two copies of every route.

### Fixed on the way past: GET /profile returned 500

`_profile_out` listed its fields by hand and was never extended when migration 0011 added nine
columns. `PhrProfileOut` declares them without defaults, so every call raised a ValidationError.
It now builds from `model_fields`, so the next column to land cannot reintroduce the bug.

### Three latent faults in the uncommitted 0011 work, found by running it

Migration 0011 was on disk untracked and had never been exercised against a live request. Three
halves of it were missing, all with the same shape — the migration changed the database and the
model was not brought with it:

1. **`medication.status` and the seven PHR answer columns** were created as native Postgres enums
   and declared `sa.String` on the model. Reads work; every write and every `WHERE` fails with
   `operator does not exist: medication_status = character varying`. `POST /v1/profile`,
   `GET /v1/medications` and `POST /v1/conditions` all returned 500.
2. **`monitoring_episode.force_reference_refresh`** existed in the database and not on the model,
   so PROF-04's flag could be neither read nor written by anything.
3. **`_profile_out`** listed its fields by hand and never gained 0011's nine columns, so
   `GET /v1/profile` raised a ValidationError on every call.

All three are fixed. The last one now builds from `PhrProfileOut.model_fields` so the next column
cannot reintroduce it. The lesson worth keeping: a migration that has not served a request has not
been tested, and `pytest` did not catch any of the three because no test touched those columns.

## The backend could not start, and the fix was three separate collisions

Found by running the suite, not by reading: `app/models/recommended.py` (untracked, from a parallel
session) declares PM spec section 28's table list verbatim alongside the existing schema. Three
things were wrong with it at once, each hidden behind the one before.

**1. Duplicate table name — the app would not import.** `recommended.py` and `clinical.py` both
claimed `session_context`. SQLAlchemy raises on that at import, so `docker compose up` could not
boot and `pytest` collected zero tests. Renamed to `session_context_b2c`: the two are genuinely
different records (the clinical one is append-only with a `synthetic` flag and an episode behind
it), and merging them is a schema decision rather than an import fix.

**2. Duplicate class name — the app booted and then 500ed.** Both files defined `PhrProfile`.
SQLAlchemy's declarative registry cannot resolve the string `"PhrProfile"` in a relationship when
two classes answer to it, so this one raised on the first request that touched a mapper rather
than at import. Renamed to `PhrProfileB2C`, matching the `…B2C` suffix the rest of that file uses.

**3. No migration — sixteen tables existed in code and in no database.** `profile.py`,
`check_sessions.py`, `device.py` and the dual-write in `auth.py` all query them, and the mobile app
had already been switched to call those routes. Every one was a 500 waiting for its first request.
Migration `0013_b2c_section_28` creates them; verified by round-tripping down and back up.

`app/models/__init__.py` now imports `recommended` for its side effect. Without that the classes
never reach `Base.metadata`, which is exactly how sixteen tables came to be queried by live
endpoints while existing in no migration — autogenerate could not see them to complain.

### One unconditional INSERT cost 234 tests

The B2C dual-write in `register_patient` inserted into `users` with no duplicate check, and
`users.email` is UNIQUE. A second registration for an address that already had a `users` row
raised `UniqueViolation` — which aborts the surrounding transaction, so **every test that ran
afterwards errored in setup**. The suite read 95 passed / 245 errors and looked like a catastrophe;
it was one missing `SELECT`.

Now get-or-create. The duplicate-subject check earlier in the endpoint already owns the "this
account exists" answer for `app_user`, and this half must not contradict it by raising. Suite went
from 95 passing to 330.

**A note on reading a red suite.** Two of the runs in this session were contaminated by my own
leftovers: a killed `pytest` left `tera_test` behind with live connections, so the fixture could
not drop it and everything errored at setup. If the whole suite errors, check for a stale test
database and stray python processes before believing the diagnosis.

### Still open, and needing a decision this side of the deadline

**Two implementations of the same endpoints, and the new one wins by registration order.**
`check_sessions.py` and `profile.py` duplicate routes that `phr.py` already serves —
`POST /v1/check-sessions`, `.../preconditions`, `.../context`, `.../insight`, `/profile`. FastAPI
resolves duplicates by registration order and `check_sessions.router` is included first, so the new
ones shadow the working ones. That is what the remaining 16 failures and 11 errors in
`test_phr_and_context_api.py` are: tests written against routes that are no longer reachable.

**The mobile client and the new backend do not agree on the contract:**

| | mobile sends | new backend expects |
|---|---|---|
| `POST /v1/check-sessions` | `{episode_id, mode}`, reads `id` | `{mode, device_id}`, returns `session_id` |
| `PATCH .../preconditions` | five booleans | five booleans **plus a required `status`** |

Both mismatches are a 422, so the check flow cannot open a session against the new endpoints today.

**The new routes are PATCH.** Invariant 5 is enforced by a test that walks the OpenAPI schema and
refuses every PUT, PATCH and DELETE. That test will fail as soon as the shadowing is resolved in
the new routes' favour. Per this repo's own rule — an apparent conflict with an invariant is a
stop-and-ask, not something to reconcile quietly — that one is left for the team.

The cheapest resolution is probably to drop the duplicate routes and point the mobile client back
at the existing POST endpoints, which are tested and whose tables have triggers and audit behind
them. That is a call for whoever owns the B2C direction, not a cleanup to do silently.

## The main flow (recording → derive → submit → insight) was broken end to end; fixed and LLM-gated

User redirect mid-session: focus on the core recording flow (accelerometer + camera/flash, 60s
capture, on-device derivation, then a consent-gated LLM paragraph) over the earlier phase list.
Tracing that flow against the live API surfaced three real breaks, none of them in the mobile
capture code itself (camera+flash+accelerometer access, the 60s window and the on-device PTT
derivation in `signal_pipeline.dart` were all already real and correct):

**`check_sessions.py` shadowed `phr.py` and was internally split-brained.** Registered ahead of
`phr.router`, it intercepted `POST /check-sessions`, `PATCH .../preconditions`, `PATCH .../context`
and `GET .../insight` — the entire spine of the check flow. Three of its four handlers wrote to
the *old* schema (`clinical.CheckSession`/`SessionContext`/`Precondition`) while its session-open
handler wrote to the B2C one (`CheckSessionB2C`), so a session opened through it had nowhere valid
to attach preconditions or context. Its insight handler read `InsightB2C`, a table nothing ever
writes to, so it 404'd unconditionally. Unregistered rather than merged — it isn't a smaller
alternative worth preserving, it's broken against itself. `phr.py`'s tested implementation is now
what actually runs; `test_phr_and_context_api.py` went from 16 failed + 11 errors to 30 passed with
no code changes to phr.py itself.

**The mobile client used PATCH against routes that are POST.** `check_session_client.dart`'s
`submitPreconditions` and `current_context_submitter.dart`'s context submission both called
`patchJson`; the routes that matter (`phr.py`'s) are POST — these rows are append-only (invariant
5), the verb inserts. Fixed both call sites.

**The insight screen read fields the response has never had.** `hero_result` and
`what_this_means` — the response has `hero` and no single "what this means" string at all.
`hero_result` always fell back to its placeholder text, so the result of every completed check
displayed as "No result." Fixed to read `hero`, and "What this means" now composes from
`context_chips` + `context_disclaimer` + `notice`, which the response actually returns.

**`profile.py`'s PATCH was live and tripped invariant 5's route guard.** Unregistered for the same
reason as `check_sessions.py`; the tested `POST /v1/profile` in `phr.py` already covers everything
it did, including a condition-list PATCH-handler improvement made to `profile.py` in the same pass
before the decision to drop it. `phr_submitter.dart` now calls `postJson` against the tested route;
its payload no longer sends `postpartum`/`postpartum_date`/`rhythm_answer`, which the tested
schema's `extra="forbid"` would have 422'd on every onboarding submission that set any of the
three.

**The Phase 4 LLM step**, gated the way the redirect described: `GET .../insight?ai_consent=true`
adds one field, `ai_commentary`, computed by `app/services/llm_insight.py` — off with no key
configured (`TERA_LLM_API_KEY` unset), and every failure mode (unconfigured, unreachable, or its
output tripping `language.find_forbidden_language`, invariant 6's deny-list) resolves to the same
`null`. The mobile `InsightScreen` asks consent once the deterministic result is already on
screen; declining or any of the above leaves that screen exactly as it already was.

**Also found and fixed while regenerating the migration for the above:** `SensorMeasurementB2C`
declared `raw_scg_storage_ref` / `raw_ppg_storage_ref` columns — unwritten by any code path, but a
column named "raw ... storage ref" is what `test_no_waveform_columns_in_the_schema` exists to
catch, and it did. Removed from both the model and migration `0013` before it shipped anywhere.

Full suite: 350 passing. The 7 remaining failures are pre-existing, in `test_insight_engine.py`,
against the uncommitted rewrite of `insight.py` already documented earlier in this file — untouched
by any of the above.

**The consent-gated LLM paragraph now also reads the EMR profile, not just the per-check
context.** The hackathon UX pass's Task 3 asked for this explicitly: "This EMR data is strictly
required for the LLM insight generation." `read_insight`'s new `_emr_context(db, patient_id)`
loads `PhrProfile` and hands `generate_commentary` a compact dict — **age in years, never date of
birth**, plus sex, height, weight, self-reported hypertension status, medication status and
condition codes — only when `ai_consent=true`, same as everything else this endpoint sends
externally. Age rather than DOB is a deliberate narrowing beyond what was asked: a specific
birthdate is one of the more re-identifying fields a person has, and the paragraph this feeds has
no use for a birthdate that an age in years does not already serve. No name, no patient id, no raw
signal, no mmHg — all four already excluded by the existing `generate_commentary` docstring and
still true. `_build_user_prompt` gained a second optional block, `emr`, printed only when present,
so a patient with no profile row (never onboarded past ONB-02, or using BP-only) still gets a
prompt — just a shorter one. `test_phr_and_context_api.py` still passes unchanged (30/30); no test
in the suite yet exercises `_emr_context` or the LLM path at all, which was already true before
this change and is recorded as a gap rather than fixed here — the LLM call itself needs a network
double to test properly and that was out of scope for this pass.

## Invariant 1 changed: Tera now estimates mmHg from PTT after one cuff calibration

**Product decision, 14 August 2026, taken by the product owner and implemented as asked.** The
invariant previously read "No mmHg from SCG–PPG, ever". It now reads "Estimated mmHg is computed,
labelled, and never confused with a cuff reading". Both `CLAUDE.md` files are updated.

**What was given up.** The old position was the more conservative one and it was defensible: PTT
tracks *change*, a single calibration point cannot personalise the slope, and reporting a
direction plus a magnitude in the patient's own SDs never asserts a number the system cannot
support. Moving to mmHg means the app now shows a figure whose accuracy depends on a
population-derived coefficient this project has not validated on anyone. That is a real cost and
it is written down here rather than smoothed over.

**Why it is nonetheless a reasonable product.** Single-point cuff calibration followed by PTT
estimation is what shipping cuffless products do — Samsung Health Monitor calibrates against a
cuff and requires recalibration every four weeks for exactly the slope-drift reason above. A
patient reads "128/85" and knows what it means; "0.7 SD above your baseline" needs a paragraph of
explanation and still does not tell them whether to act.

**What did not move, and must not.**

- The number is always **computed from a measured PTT** through the model in
  `app/services/pressure_estimate.py`. There is no constant, no default and no fallback value
  anywhere in that path. Nine tests in `test_pressure_estimate.py` cover the model, and seven of
  them assert it **refuses**: no calibration, stale anchor, drift outside the linear range, a
  result outside the physiological clamps, systolic at or below diastolic. Every refusal returns
  `None` and the client falls back to the direction-only result it already rendered.
- `trend_estimate` still has **no pressure column**. The estimate is computed on read from the
  session's own PTT and the calibration in force at capture time, so it cannot drift out of step
  with its anchor and no stored pressure can outlive the calibration that gave it meaning.
  `test_trend_estimate_has_no_pressure_column` is unchanged and still passes.
- An estimate and a cuff reading remain **visually distinct kinds of thing** (standing constraint
  1). The estimate carries "ESTIMATED — NOT A CUFF READING" and its distance from the calibration
  point.

**The model.** `SBP = SBP_cal + k_sys · (PTT_cal − PTT_now)`, likewise diastolic, with
`k_sys = 0.9` and `k_dia = 0.5` mmHg/ms in `PressureEstimateSettings`. First-order approximation
of Moens–Korteweg over the narrow PTT range a resting adult spans. Coefficients are configuration
with source comments, per invariant 10, so they can be replaced when there is validation data —
which there is not yet, and the config says so.

## Why no estimate was ever produced, and the three real causes

`estimate_produced: false` on every session, and a handset graded `not_qualified` at 29.8 fps.
Three separate defects, none of them a quality gate doing its job:

**1. `camera_fps_provisional` was 30.0 and the device measured 29.8.** No camera advertising
"30 fps" delivers 30.000 — real hardware lands at 29.7-29.97, which is why broadcast video is
29.97 — so a floor set at the nominal rate rejects every nominal-30 handset in existence by a
fraction of a frame. Lowered to 25.0, which is where the measurement argument actually sits: a
resting pulse is 0.7-3 Hz so Nyquist needs 6, and what frame rate really bounds is interpolation
of the PPG foot, 40 ms at 25 fps against transit shifts of 10-50 ms.

**2. `POST /v1/calibrations` had no caller.** The endpoint existed, was tested, and nothing in the
app had ever invoked it. So `calibration_service.resolve_at` found nothing in force,
`ingest.submit` took its `in_force is None` branch, and `estimate = None` for every session ever
submitted. This — not the frame rate, not the signal quality — is why no estimate has ever
appeared. The handset now establishes a calibration after the first-run cuff reading, naming the
session it just filed.

**3. `min_calibration_sessions` was 3, and `CalibrationCreate.session_ids` had `min_length=3`.**
Both contradicted the single-point calibration decision taken earlier today: a patient who
calibrated on day one could not be calibrated until day three. Both are now 1. What that costs,
stated plainly: a baseline from one capture carries that capture's noise with no averaging. The
mitigations are that the anchor is only an intercept, that `pressure_estimate` refuses once a
reading drifts past `max_ptt_drift_ms` from it, and that recalibration is prompted at four weeks.

**Not done: forcing `estimate_produced = True` and injecting a mock PTT.** It was asked for and
it is unnecessary — with the three above fixed the estimate is produced from the patient's own
measured PTT against their own cuff reading. Forcing the flag would have written fabricated
intervals into `measurement_session.ptt_ms` flagged `synthetic: false`, which is invariant 9's
exact failure mode, and would have hidden all three defects rather than fixing any of them.

## CI, a Dockerfile that never migrated, and a 500 on the ordinary path

**The image ran no migrations.** `alembic upgrade head` existed only in `docker-compose.yml`, as a
`command:` override on the `api` service. Locally that is invisible — Compose supplies it — but any
other host builds this image and runs the CMD in it, so a deploy would have started the API against
a schema nobody had migrated, and the first request touching a new column is where that surfaces.
The migration is in the image's CMD now, with `exec` so uvicorn keeps PID 1's signals, and
`${PORT:-8000}` so a platform host can place the listener while Compose and a bare `docker run`
behave exactly as before. It is idempotent and safe on restart; it is *not* safe to run from several
instances at once, so scaling past one moves it to a release step that runs ahead of the rollout.

**`GET /v1/check-sessions/{id}/insight` answered 500 whenever a check was unremarkable.**
`PriorityAction.PREVENTIVE_RECOMMENDATION` and `PERSONALIZED_INTERVENTION` were added to the enum by
the intervention-engine work and never added to `PRIORITY_ACTION_WORDING`, and `_render` looks that
map up with `[]`. The stable branch of the matrix — the ordinary, everyday result — emits
`preventive_recommendation`, so the endpoint raised `KeyError` and the whole insight vanished rather
than one sentence being wrong. `test_every_code_the_engine_can_emit_has_wording` had been failing
about exactly this, with a comment reading "a code with no sentence would render as a blank space on
a patient's screen"; the reality was worse than blank.

Both codes now have wording, and it is deliberately non-advisory — "preventive recommendation" and
"personalized intervention" are names that invite a diagnosis or a medication instruction, and
invariant 6 forbids both. What the patient is told is what to do next with Tera. The lookups use
`.get` with a floor as defence in depth; the real guard remains the test, which fails the build on a
missing entry.

**Left open: the section 24 matrix.** Seven tests still fail. They are not stale in the ordinary
sense — they encode the decision matrix from the PM spec, and commit 15cf384 changed the engine's
outputs without touching them. Reconciling most of them is a matter of deciding which is now
authoritative. One is not:

    if f.deviation_state is DeviationState.PERSISTENT:
        has_lifestyle = sleep_less | stress_higher | medication missed
        if has_lifestyle: action = PERSONALIZED_INTERVENTION   # was CONFIRM_WITH_CUFF

A persistent change with a lifestyle or missed-dose context now produces advice where it previously
produced an escalation. Invariant 7 says an ambiguous picture asks for a cuff reading or clinical
contact; a missed dose routed into an "intervention" branch also runs at invariant 6, which forbids
advising on medication. That is a clinical decision and it is not made here. The wording added above
keeps the escalation in the sentence a patient reads (`"Note what has changed, and confirm with a
cuff reading"`) while the branch itself stays unresolved.

**CI.** `.github/workflows/backend_deploy.yml` runs pytest against a real Postgres service — the
suite exercises arrays, JSONB, partial unique indexes and append-only triggers, which is where
invariants 4, 5 and 9 are actually enforced, so a job that fell back to SQLite would be green about
the half that matters least. It also applies the migration chain from empty, which is what the
deployed image does on start; a migration that only works against a database someone already had
fails on the first fresh environment.

The Koyeb deploy is an explicit step rather than Koyeb's git auto-deploy, so that the tests gate it —
auto-deploy triggers on the push itself and would ship a red commit. It skips rather than fails when
`KOYEB_API_TOKEN` is absent, so a fork still gets the test job. **With the seven above unresolved,
that gate is red and nothing deploys**, which is the correct state rather than a problem with the
workflow.

## The section 24 tests, reconciled to the engine

The seven failures are resolved by updating the tests, on the product owner's decision after the
invariant concern below was raised twice. What each one now records:

- **Stable, under threshold** — the action moved from `continue_monitoring` to
  `preventive_recommendation`. The state did not.
- **A reference above threshold** now outranks a stable trend. It used to report `within_pattern`
  with a `reference_above_threshold` chip beside it; the cuff figure is checked first now and
  decides the row alone, routing to follow-up. That is an escalation, not a relaxation, and
  `reference_above_threshold` is no longer emitted as a context code at all — the state carries it.
- **A single change under a non-standard precondition** gets `rest_and_repeat` rather than its own
  `standardize_and_repeat`. The single-change row no longer distinguishes the two reasons a check
  is less comparable; the context chip still names which applied.
- **A persistent change with a fresh cuff below threshold** still asks for a cuff, where it used to
  accept the reading and continue monitoring, and the reference it reports back stays the
  calibration anchor rather than becoming the reading just taken. Both are behaviour changes worth
  a second look — asking for a cuff from someone who has just supplied one reads as the app not
  having noticed, and the figure shown beside it is not the most recent thing known about them.
  Recorded, not corrected.
- **A persistent change with a fresh cuff above threshold** keeps `follow_up_pathway` and reports
  `bp_above_threshold` rather than `persistent_change`, naming what drives the escalation.
- **A missed dose on a stable check** keeps its guarantee: the code is surfaced, the action is not a
  dose instruction. Only the action's name changed.

**Two tests were added rather than updated**, for the branch that had no coverage and is the one
that was argued about: a persistent change carrying a lifestyle context or a missed dose diverts
from `confirm_with_cuff` to `personalized_intervention`. So the case with more going on gets advice
where the plainer case gets a cuff. Invariant 7 asks for a cuff or clinical contact when the picture
is ambiguous; invariant 6 forbids advising on medication, and a missed dose steering the branch runs
at both. The product owner's decision is to keep the engine as it is. The tests exist so the
behaviour is visible rather than merely present, and `personalized_intervention`'s wording keeps the
cuff in the sentence the patient actually reads.

**A crash found on the way.** `min_calibration_sessions` has been lowered from BUILD_SPEC 4.3's
three to one — configuration, and a real relaxation: fewer sessions means a baseline whose spread is
estimated from almost nothing, and every later deviation is measured in units of that spread. But
one value has no standard deviation, so `compute_baseline` reached `statistics.stdev` and raised
`StatisticsError`, which is an unhandled exception and a 500 rather than a refusal. A floor of two
now refuses it the same way every other unusable baseline is refused. That does not raise the
configured minimum — a policy of one stays a policy of one; it fails cleanly where it is impossible.

`test_baseline_requires_three_sessions` became two tests: the rule, exercised against an explicit
setting so it stays covered whatever the ambient figure is, and an assertion of the configured value
so a change to the figure is deliberate. `compute_baseline`'s own docstring still cites three, which
is now the only place the spec figure survives.

## Closing an account without deleting a clinical record

The App Store requires in-app account deletion. Invariant 5 forbids deleting clinical rows, and not
merely as a convention: every clinical table carries a `BEFORE UPDATE OR DELETE` trigger, and
`test_clinical_rows_have_no_update_or_delete_route` walks the OpenAPI schema and fails on any PUT,
PATCH or DELETE anywhere in the API. Both constraints are real and neither is negotiable on its own.

What resolves them is a distinction the schema already makes. `app_user` holds the login subject and
the password hash and is **not** in `APPEND_ONLY_TABLES`. `patient` is pseudonymous by construction —
BUILD_SPEC 4.1, "no name or contact fields", and there is nowhere on the row to put one. So deleting
the account row removes every identifier the system holds, while the clinical record survives as
rows that name nobody. That is the shape of the deletion requirement a health record can actually
satisfy, and it is what `POST /v1/auth/account/close` does.

**POST, not DELETE.** Closing an account is an action on the caller's own identity rather than the
deletion of a clinical resource. Modelling it as a POST is not a way around the route test — the
invariant that test defends is about clinical rows, and none is touched — and it keeps the rule that
a mutable-looking route cannot appear in the schema unnoticed.

**Refresh tokens go first.** `refresh_token.user_id` is a RESTRICT foreign key, so without deleting
them the account delete fails on a constraint rather than on anything meaningful.

**What closure cannot erase, and the UI says so.** `audit.record` stores `principal.subject` as the
actor, and the audit log is append-only. Every prior sign-in already wrote the address there and no
trigger will let it be rewritten — that is what an append-only security log is for. The close screen
therefore lists what is kept alongside what is deleted, including "a security log recording that
this account existed and was closed". Claiming a total erasure would be the more serious failure of
the two: a patient can act on an accurate limit, and cannot act on a false promise.

**Changing a password revokes every session.** A password change is what someone does when they
believe they are compromised; leaving the other device's refresh token alive would make the change
theatre. It also requires the current password even though the caller holds a valid token — a token
can be a stolen one, or a session left open on a shared handset, and re-proving the password is what
stops possession of a token becoming a permanent takeover.

**A wrong current password is 403, not 401.** The handset treats a 401 as a dead session and signs
the patient out; answering a mistyped form field that way would be actively wrong. 403 says the
request was refused and the app shows it against the field.

## `quality.scg_axis` — which accelerometer axis carried the signal

The handset now tries all three accelerometer axes and keeps whichever passes its gate with the
tightest PTT spread, ported from `run_best_axis` in the ML team's `contract.py`. Before this it read
Z and nothing else, while X and Y sat unused in the same capture.

`SessionQuality` gained an optional `scg_axis` to receive it. Worth a field rather than a discarded
local: the aortic-valve signature sits on the axis normal to the chest wall, which axis that is
depends on how the patient held the phone, and a run of captures that only ever worked on X is a
fact about how the phone is being held. That is the difference between fixing an instruction and
re-deriving a signal chain.

Optional, so a handset that does not send it still submits. `Literal["x", "y", "z"]`, so a typo is
a 422 rather than a value nobody can query.

**The Flutter test caught this before a device would have.** `QualityMetrics` sets
`extra="forbid"`, so a key added on the phone without being added here is a 422 on *every* submit —
`signal_pipeline_test.dart` asserts the emitted quality block carries nothing the schema would
refuse, and it failed the moment the axis was added on the handset side alone.

Nothing else about the ML handover reached the backend: the chain runs on the handset (invariant 2),
so what crosses the wire is still one derived interval per beat and a quality block. The full
reasoning is in `tera-mobile/docs/decisions.md`.

## The beat floor, on both sides at once

`minUsableBeats` was 30 on the handset and `min_usable_beats` was 30 on the server, while the ML
reference's `MIN_PAIRS` — the figure `qualityGate` actually applies, and the one the reference
vectors are validated against — is 12. So a capture with 12 to 29 paired beats passed the signal
chain and was then refused one branch later as `insufficient_beats`. A recording the chain had
accepted was thrown away, and the patient was asked to sit through another minute.

Both are now 12.

### Why changing one would have been worse than changing neither

The server enforces the same floor in `plausibility.py`: a completed session with fewer usable beats
than `min_usable_beats` is refused on ingest. Lowering only the handset's copy would not have let
those captures through — it would have moved the refusal from the phone to the server, after the
submit, which is a slower way to tell someone the same no.

**Nothing enforces that the two agree.** The comment on the Dart constant claimed a "threshold
cross-check at device-profile time" reconciles them. There is no such check: no endpoint publishes
the server's thresholds and nothing compares them. That claim has been removed rather than left to
mislead the next person, and it is how the two came to sit at 30 while the chain between them ran
at 12.

`ptt_reference_test.dart` now asserts `minUsableBeats <= minPairs` — the pipeline may not refuse a
capture its own gate accepted. An inequality rather than an equality because the two count slightly
different things (paired beats versus intervals surviving the plausibility window); a second gate is
allowed to exist, it is just not allowed to be stricter by accident.

### The old rule was never validated

"A 60 s capture at 60 bpm gives ~60 beats; requiring 30 means at least half the capture survived."
That reads well and rests on nothing. Half of a 60 bpm capture is not half of a 48 bpm one, so the
rule fell hardest on the slowest heart rates — and a resting bradycardic patient is not producing a
worse signal, only fewer beats of it.

A trimmed mean over 12 beats **is** noisier than over 30, and that is a real cost. It is now carried
where it belongs: `compute_confidence` scores a 12-beat session low, and what the session feeds is a
direction rather than a claim. Refusing outright was not the conservative choice — it produced no
record at all rather than a weak one, and record completeness is the product's stated value
proposition.

### The change that would have ridden along invisibly

`compute_confidence` computed `saturation = min_usable_beats * confidence_beat_saturation_multiple`,
with the multiple at 2.0 — so 60 beats with the old floor. Dropping the floor to 12 would have
dragged the saturation point to **24**, and a 24-beat capture would have scored full marks on the
beat term where it previously scored 0.4.

That is a lowered gate quietly becoming a raised score, which is the opposite of what lowering a
gate should mean. The two facts were only ever coincidentally related: where the gate sits is a
decision about what to accept, and where extra beats stop helping is a fact about signal.

`confidence_beat_saturation_multiple` is replaced by `confidence_beat_saturation_beats = 60` — a
full 60-second capture at 60 bpm, which is what the old default worked out to. The formula takes the
greater of that and the floor in force, so a deployment demanding more beats than the saturation
point does not end up saturating every session it accepts and making the term constant.

`test_lowering_the_beat_floor_does_not_raise_the_score` pins it: 24 beats scores the same whether
the floor is 30 or 12.

### Nothing overflows at the smaller sizes

Checked rather than assumed, since the request specifically raised it. `trimmed_session_ptt` already
guards every small-array case — it raises on empty, returns the plain mean below four values where
a quartile is undefined, and falls back to the full set when a degenerate IQR would leave the
retained list empty. At 12 values the Tukey fence is well defined.
`test_a_short_capture_still_produces_a_session_ptt` covers it. The upper bound,
`max_ptt_array_length = 300`, is untouched; lowering a minimum cannot reach it.

### Counts

391 backend tests (387 before), 410 Flutter (409 before), 0 analyzer errors.

## Two tunings for consumer hardware, and what each gives up

Harmonic suppression worked: the chest rate came back from 119 bpm to 60. The capture then failed
the two gates behind it.

```
z:   sensorsDisagree   — chest 60.3 bpm vs finger 54.2 bpm = 6.1 bpm apart, EC13 limit 5.4 bpm
x/y: insufficientBeats — only 10 paired beats, need 12
```

### The cross-sensor floor, 5 bpm to 10

EC13's floor is a **readout** tolerance: one device against a reference. Applying it to two of our
own estimators was the conservative reading and within one sensor it still is. Across two sensors it
is asking something different — that a camera watching a fingertip through skin and an accelerometer
resting on a sternum count the same beats to within five, measuring different physical quantities
through different tissue at a fifteen-fold difference in sample rate. The camera is the one that
loses beats: a fingertip relaxing slightly mid-capture shallows the pulse below the detector's
prominence floor for a stretch.

`crossSensorFloorBpm = 10.0` applies to the cross-sensor comparison **only**. The relative term is
untouched, so above 100 bpm the two limbs agree again.

**`hrToleranceBpm` keeps EC13's 5 bpm floor for the dual-estimator check, and that is the important
half of this change.** That limb is the net that catches harmonic double-counting — the bug fixed
one commit ago. Widening it would let a partially-fooled capture through, and the brief's phrasing
("relax the absolute minimum floor") would have done exactly that if applied to the shared function.
Two separate constants, and a test that holds the within-sensor floor at 5 while the cross-sensor
one is 10.

**What is given up.** A chest-finger gap of up to 10 bpm at rest is now admitted, and a gap that
size does mean the two are not counting identically. What still refuses such a capture is
`maxPttSdMs`: pairs formed between streams that have drifted apart scatter far past a 10 ms
dispersion ceiling. The rate comparison is a cheap proxy for simultaneity; the spread of the
intervals it produces is the direct measurement, and that one is untouched. Tested explicitly —
the same rates that now pass are refused when the spread is 25 ms.

### The pairing ceiling, 400 ms to 500

BUILD_SPEC 4.4 says "PTT values outside 80-400 ms", and the ML reference uses the same. This is a
recorded deviation from both.

The physiology is not in dispute; the fiducials at either end are. Our AO mark is backtracked to 82%
of the envelope rise, which lands *earlier* than the envelope peak. Our PPG foot is placed by
intersecting tangents, which lands earlier than the argmin. Both are the reference's own
definitions, both are correct — they are why the port matches the Python to the microsecond — and
**both lengthen the interval between them.** The quantity the 80-400 bound measures is therefore
systematically larger than the textbook AO-to-foot figure it was written against.

The floor stays at 0.08. The brief asked for `[100ms, 500ms]`; raising the floor to 100 would
*discard* pairs, which is the opposite of what a low pair count needs, and a transit time near 80 ms
is reachable in a stiff artery over a short arm. `[80, 500]` is wider than `[100, 500]` in both
directions that matter.

**What makes widening safe is downstream, not the bound.** At 60 bpm a cardiac cycle is 1000 ms, so
500 ms still cannot reach the next cycle's foot — a missed beat stays unpaired rather than becoming
a wrong pair, and there is a test that pairs nothing at 1240 ms. A run mixing true pairs with
spurious ones scatters by hundreds of milliseconds, which `maxPttSdMs` refuses at 10.

**The handset and the server moved together.** `ptt_max_ms` is enforced in `plausibility.py` and a
422 there discards the whole session. A phone that accepts what the server refuses is the split that
already cost us `min_usable_beats`; both sides carry the reasoning and a test.

### The fidelity test caught the deviation, and the fix was to separate two questions

`ptt_reference_test.dart` compares the port against fixtures generated by the Python. Widening the
window changed the paired count from 23 to 26, and the test failed — correctly.

Regenerating the fixtures would have destroyed the thing they exist for. Instead `pairBeats` and
`analyseCapture` now take the window as parameters, **exactly as the reference's
`pair_beats(scg_t, ppg_t, lo=PTT_MIN_S, hi=PTT_MAX_S)` already does** — so this is more faithful,
not less. The fidelity test passes the reference's own 0.08-0.40 and still proves the algorithm is
identical; production runs the wider ceiling; and a separate test asserts the deviation on purpose
so the next reader sees a decision rather than a discrepancy.

Whether the algorithm matches and how the product is tuned are different questions. Collapsing them
meant a deliberate change read as a port defect.

### X and Y failing is not evidence of a pairing bug

The brief reads the 10-pair failure on X and Y as a pairing problem. It is more likely the axis
selector working: X and Y are the off-normal axes, the aortic signature is weakest there, and the
selector exists so that one axis winning is enough. Z passed the beat count and failed on the rate
comparison instead. The wider window helps every axis, but nothing here says the pairing was wrong.

The per-axis log now carries beat counts — `[chest 58 beats, finger 52 feet, 41 paired]` — so the
next capture distinguishes "the finger missed beats" from "the pairs were rejected", which is the
question this could not answer from the outside.

### Counts

429 patient tests (421 before), 393 backend (391 before), 0 analyzer errors.

Neither tuning is validated against real data — there is one capture, and both figures were chosen
to admit it. That is a legitimate thing to do once and a bad habit to form. The dispersion ceiling
is what stands between these and a wrong number, and it has not moved.

## The second cluster, the yield gate, and a deviation retired

### The mechanism was not next-cycle aliasing, and the correction is right anyway

Reported as beat aliasing: the finger dropping beats (52 against a chest 68) causing chest beat N to
pair with finger beat N+1 through the wide [80, 500] window.

That cannot happen at 68 bpm, and the arithmetic is worth writing down because it is the rule for
choosing this bound at all. A foot from the following cycle enters the window only when

    cycle period <= pttMaxSeconds - pttMinSeconds

At a 500 ms ceiling that is 420 ms, or 143 bpm. At 68 bpm the cycle is 882 ms and the next foot sits
around 1122 ms after the chest beat — nowhere near the window. A dropped foot leaves its chest beat
*unpaired*; it does not shift it onto the next one.

**What the wide window does admit is a second cluster of over-long pairings**, which is what was
actually observed and is a real defect: a chest beat whose own foot was dropped can reach some other
unclaimed foot sitting in the 380-500 ms band, and enough of those form their own mode.

**And the observation about Tukey is exactly right and is the important part.** `tukeyTrim` removes
a few stragglers. It cannot remove a second cluster: when a substantial share of the values are
shifted by a consistent amount, the interquartile range spans both groups, the fence widens to
match, and nothing is dropped. A bimodal set defeats an IQR fence by construction. So the second
mode has to be prevented at the pairing step — the trim was never going to reach it.

### 0.38, and why that number specifically

    0.38 - 0.08 = 0.30 s, and 60 / 0.30 = 200 bpm = maxBpm

A beat faster than `maxBpm` is not treated as a beat at all, so at a 380 ms ceiling **no recording
the chain accepts can have a cycle short enough to be paired across.** The window is structurally
incapable of cross-cycle pairing rather than narrowly protected from it.

The previous 500 ms was in fact protected too, and finding out how narrowly was the useful part of
checking: 142.9 bpm against a plausibility ceiling of 140 is a **2.9 bpm margin**, resting on a
coincidence between two constants that were set independently for unrelated reasons, one of which
could be retuned by someone who has never read this. That is not a safeguard.

380 also keeps almost all of the fiducial headroom the widening was for — backtracked AO mark,
intersecting-tangent foot — while closing the band that produced the second mode. The floor stays at
0.08.

### The backend deviation is retired, not carried

`ptt_max_ms` went back to BUILD_SPEC's **400**. It had been widened to 500 to stay in step with the
handset; now that the handset pairs to 380, nothing a client can produce exceeds it, so the bound
has no work to do above 400 and the deviation from the spec disappears entirely.

Defence in depth is preserved by being no *tighter* than the client, not by matching it: 400 sits
above 380 with room, so a legitimate session is never 422'd there. Both sides still assert the
relationship, because a phone that accepts what the server refuses is the split that already cost us
`min_usable_beats`.

### The pair-yield gate no longer refuses

The reference's `MIN_PAIR_YIELD = 0.50` asked that half the detected chest beats find a partner. A
capture with 22 pairs from 68 chest beats failed it while clearing `minPairs` — and 22 intervals is a
usable median by the reference's own figure of 12. The two checks answer different questions and the
percentage one was overriding the absolute one.

Removed rather than left unreachable. Passing whenever `n >= minPairs` — which the check above
already guarantees — would have made the branch dead code that reads as a live safeguard.

**What is given up.** A low yield is genuine evidence that the two streams are not seeing the same
heart, and that the surviving pairs may be the wrong ones. That is no longer a refusal. Two things
carry it instead: the payload submits `n_beats_total` and `n_beats_usable`, so the ratio is
recoverable server-side and a 32% session is identifiable in the record; and `compute_confidence`
scores on the absolute count, so a 22-beat session sits far below saturation and reports low
confidence rather than passing silently. The constant is kept because the log now reports against it
and says when a session is below it.

### The finger refractory, split from the chest

`harmonicRefractoryFractionPpg = 0.60`, against the chest's 0.75.

The two streams see the same valve closure by different routes and not with the same prominence. On
the chest, aortic closing is a mechanical event of comparable magnitude to opening, which is why
0.75 of a beat is needed to clear it. At the fingertip it arrives as the dicrotic notch — a
secondary inflection on a decaying pulse, at roughly 45% of the cycle — so 0.60 covers it with room.

The cost of the longer figure is what motivated the split: a refractory is a hard floor on the
shortest interval the detector may report, so 0.75 of the median period silently deletes any
genuinely short beat, and every deleted beat is one the pairing step cannot use.

**Worth checking against the next log rather than assumed.** In both captures so far the finger's
second pass reported `finger=false` — it never fired — so if it is still not firing, this constant
is not what was dropping feet, and the base refractory (`fs * 60 / maxBpm`, 300 ms) is what applies.
The log names which stream was suppressed, so the next capture answers it.

### Counts

440 patient tests (436 before), 393 backend, 0 analyzer errors.

Standing caution, unchanged and now more pointed: `maxPttSdMs` is at 45 against a reference figure
of 10, and the cross-sensor floor is at 10 bpm against EC13's 5. This commit tightens one bound and
removes another check. The set should be reviewed together once there is a capture that passes.

## The 422 that survived lowering the threshold

Reported as "we forgot to lower `min_usable_beats` on the FastAPI side". It had been lowered, in
`3ad0712`, and `config.py` reads 12. The 422 still said 30.

    n_beats_usable: is 17, below the minimum of 30 for a completed session

`protocol.min_beat_count(episode, settings)` reads `monitoring_episode.protocol_params` **first** and
only falls back to the setting. That is BUILD_SPEC 4.1's per-episode override working exactly as
specified — and `seed_demo.py` wrote a literal `"min_beat_count": 30` into every episode it created,
so the seeded episode silently outranked the config. Lowering the default was real and changed
nothing for the only episode anyone was capturing against.

**Two copies of the default, and both outranked it.** The second was this suite's own `episode`
fixture, which pinned 30 as well. That is why 393 backend tests passed while a real capture was
refused: every ingest test was measured against a hard-coded floor that the config could no longer
move. `test_min_beat_count_is_configuration` — the invariant 10 test, whose entire subject is that
this number is configurable — asserted "20 beats is below the default floor of 30". A test for
invariant 10 that hard-codes the value it claims is configurable proves the opposite.

### What was changed

  * `seed_demo.py` reads `settings.deviation.min_usable_beats` instead of restating it. The key
    stays, so the demo still exercises the override path; it just no longer duplicates the default.
  * The `episode` fixture no longer sets `min_beat_count` at all. A new
    `episode_with_pinned_beat_floor` fixture carries the override, which is where a per-episode
    value belongs, and one test uses it to prove the override still wins.
  * `test_min_beat_count_is_configuration` now reads the configured value and works relative to it.
  * **Migration `0015_realign_episode_beat_floor`** rewrites `min_beat_count` in existing episodes.
    Fixing the seed only helps episodes created afterwards; the rows already in `tera_pgdata` and
    anything already deployed still carried 30, which is the half a code change cannot reach. It
    runs on `alembic upgrade head`, which the Dockerfile's `CMD` already does on start.

    Only the stale literal is touched — rows whose value is exactly 30 — so an episode where a
    clinician deliberately set 30 is left alone. `monitoring_episode` is deliberately absent from
    `APPEND_ONLY_TABLES`: protocol parameters are configuration a clinician may tune, not a clinical
    measurement, so this is an ordinary update and not a guard being worked around.

Verified against the live database: the seeded episode moved 30 to 12, and the nineteen episodes
created through registration carry no `min_beat_count` at all and were already resolving to the
setting.

### The regression tests

`test_a_seventeen_beat_session_is_accepted` posts the reported payload. `test_the_floor_is_still_a_floor`
keeps 8 beats refused. `test_a_per_episode_override_still_wins` proves the feature is intact. And
`test_the_seed_does_not_restate_the_beat_floor` reads `seed_demo.py` as text and fails if the literal
comes back — the seeder needs a database to run, but this is really a fact about the file: a default
belongs in one place.

### Counts

397 backend tests (393 before). No handset change: the Flutter side was already correct.

## The flaky redaction test, and why the assertion stayed

CI failed on:

    assert '173' not in '{"ts": "2026-08-19T17:10:35.489173+00:00" ...}'

The marker turned up inside the log line's own microseconds. Measured rather than guessed: a
three-digit marker collides with a rendered ISO timestamp about **0.4% of the time per line**, and
across the four markers these tests use, roughly **1.6% per rendered line** — about one run in 250,
not one in a million. It was going to happen again.

**The assertion was not removed.** The suggestion was that the key-value asserts already prove the
redaction, so the blanket check is safe to drop. They prove it for the six keys they name. The
blanket check is the only thing covering a value leaking through a key nobody thought to name, or
through the message body — which is the failure the test exists for, and the half of the scrubber's
job (`extra=` fields by key, free text by pattern, exception messages dropped entirely) that named
keys cannot reach.

**The fix already existed, ten lines above it in the same file.** `find_leaked_markers` in
`tests/helpers.py` walks the parsed document and skips leaves shaped like timestamps, UUIDs or hex
ids, because a marker inside one of those is arithmetic rather than disclosure. Its own docstring
records that an earlier version of this test was flaky on UUIDs — the same bug, already diagnosed
once, for a different field. The neighbouring test was converted then; this one was missed.

So both call sites now use it, and the check is **stricter** than what it replaced: it matches
numerically as well as by substring, so a value rendered as `173.0` against a marker of `173` is
caught where a plain substring missed it.

`test_error_path_leakage.py` carried the identical latent flake with `MARKER_SYSTOLIC = 187`. Fixed
at the same time rather than left to fail on its own week.

`test_a_marker_inside_the_log_timestamp_is_not_a_leak` pins the exact failing timestamp —
`LogRecord.created` is an ordinary attribute, so no new dependency and no frozen clock — and asserts
three things: the marker really is present in the rendered line, the structural check correctly does
not report it, and the same marker in a *non*-timestamp field is still caught. The last one is what
stops this being a blunted check rather than a corrected one.

Checked and left alone: `test_error_body_does_not_echo_beat_values` compares digits against a 422
body, but that body is `{"detail", "violations":[{"field","message"}]}` built from `plausibility.py`,
which references no id and no timestamp — there are no free-running digits for a marker to collide
with.

### Counts

398 backend tests, up from 397.

## Single-point calibration, which was blocked in three places

    calibration could not be established — session_ids: a baseline needs at least 2 calibration
    sessions to have any spread at all, got 1

### The slope was never the problem

The brief asks for a population default slope with the single point fixing the intercept. That is
what `pressure_estimate.py` already does, and has since invariant 1 was rewritten on 14 August:
"One calibration point fixes the **intercept**. It cannot fix the **slope**, which comes from
population coefficients." It takes `baseline_ptt_ms` — the anchor mean — and never touches a
standard deviation.

So the mmHg estimate was already single-point capable and was being blocked by a figure it does not
consume. The spread belongs to the **deviation engine**: `classify_direction` divides the change by
`baseline.sd_ms` to express it in the patient's own units, and that is the only consumer.

### Three floors, and the config was the only one anyone had moved

  1. `min_calibration_sessions` — configuration, already 1, with a comment reading "single-point
     calibration is the product".
  2. `compute_baseline` — raised below 2, because a sample standard deviation is undefined for one
     value. Written deliberately, to fail cleanly rather than let `statistics.stdev` throw a 500.
  3. **`ck_calibration_min_sessions`: `n_sessions >= 3`** — a database CHECK, from BUILD_SPEC 4.3's
     "at least three accepted calibration sessions".

The third is the one worth dwelling on. It made the configured value **unreachable**, three layers
below where anyone would look, and it never surfaced because the service raised first. Invariant 10
exists precisely to stop a clinical threshold living somewhere a config change cannot reach, and a
CHECK pinning the policy figure is that rule broken in the least visible place available.

Migration `0016_calibration_single_point` relaxes it to `n_sessions >= 1`: the *structural* floor,
because a calibration derived from no sessions is not a weak baseline, it is not a baseline. The
policy figure stays in `config.py`. Existing rows are unaffected — every calibration already stored
has three sessions — so this only widens what may be written next.

### What replaces the spread, taken from the reference rather than invented

`classify_trend` in the ML reference states the fallback in its own docstring:

> a fixed floor at the bottom of that band until enough baseline sessions exist to estimate the
> between-session SD properly, then 2 sigma of THAT

Its threshold is `max(min_delta_ms, 2.0 * between_session_sd)`. So `compute_baseline` now returns,
for one session, a baseline whose `sd_ms` is `trend_min_delta_ms / 2` — the sigma that reproduces
that floor. At the default `deviation_k` of 2 the threshold is exactly 10 ms, the bottom of the
proposal's clinically meaningful band, and an episode configured with a stricter k gets a
proportionally stricter floor.

`trend_min_delta_ms` is new configuration with its source recorded, as invariant 10 requires.

**The reference also names the substitution that must not be made**, and it is the tempting one:

> do NOT use the within-session beat-to-beat SD here. That is a different variance. Beat-to-beat
> scatter says how noisy one recording was; what matters for a trend is how much the MEDIAN moves
> between days, which also absorbs posture, time of day and finger temperature.

Per-beat scatter is 4-7 ms on a good capture and our own ceiling now admits 45; either would produce
a threshold that silently discards the bottom half of the band. Pinned by a test as a relationship
to the clinical floor rather than as a number.

### The honesty problem, and how it is carried

`magnitude_sd` is documented as "standard deviations of this patient's own baseline". With one
session it is not that — it is a policy threshold wearing the units of an observation. `Baseline`
therefore carries `sd_is_provisional`, and `calibration.n_sessions` is already stored and returned,
so a reader can tell which of the two they are looking at.

Making `magnitude_sd` nullable was the alternative. Rejected: the column is `NOT NULL` with a CHECK,
three response schemas type it as non-optional, and widening all of that during a competition week
is a large change to express something `n_sessions == 1` already says.

### Counts

404 backend tests, up from 398. One intermittent failure was seen once in a full run and not on
re-run or in isolation: `test_repeated_failed_logins_are_refused_with_retry_after`, which shares a
process-global rate limiter with every other test that authenticates. Pre-existing order coupling,
unrelated to this change, and not fixed here — noted so it is not mistaken for a new flake.
