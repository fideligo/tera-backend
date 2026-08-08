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

## Deliberate limitations

### Rate limiting is per-process

`FixedWindowRateLimiter` holds counters in process memory, so with N API workers the effective
ceiling is N times the configured one. Acceptable for a single-instance demo, not acceptable for
production. §4 asks for Redis to be justified before adding it, and a shared counter store is that
justification if this ever runs multi-instance — the limiter interface is deliberately narrow so
swapping the backing store touches one class.

### Refresh tokens are not revocable

There is no server-side denylist, so a leaked refresh token stays valid for its 14-day TTL. Out of
scope for Phase 1; the fix is a `jti` denylist table checked on refresh.

### The device eligibility bands are engineering thresholds, not validated benchmarks

`DeviceEligibilitySettings` is reasoned from the measurement requirement — the PTT differences
being tracked are milliseconds to tens of milliseconds, so sampling interval and clock stability
have to sit well below that. None of it has been validated against hardware. Phase 3's profiler
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

## Pending: Postgres-backed rate limiting (C5, not yet implemented)

Recorded before implementation so the reasoning is not re-derived. **Deferred deliberately**:
the judged demo is a working product on screen, and rate-limiting depth is correct work at the
wrong time. This entry is the design, not a description of shipped code.

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
`ingest_rate_limit_*` fields are the pattern to follow.

---

## Environment notes

The Compose Postgres publishes on host port **5434**, not 5432 or 5433 — both were already taken
by other Postgres instances on the development machine. Change it in `docker-compose.yml` and
`backend/.env` together if that does not suit.
