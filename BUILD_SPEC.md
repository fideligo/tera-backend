# Tera — Build Spec

Working spec for implementation. Read this fully before writing code. Create `CLAUDE.md`
at the repo root summarising the non-negotiables in section 2 so they persist across sessions.

---

## 1. What Tera is

Tera is a **hybrid, cuff-referenced home blood-pressure monitoring system** for adults with
diagnosed but uncontrolled hypertension whose treatment has recently been adjusted.

A smartphone records two synchronised signals: seismocardiography (SCG) from the accelerometer
resting on the sternum, and photoplethysmography (PPG) from the rear camera with a fingertip on
the lens and the torch on. The interval between aortic valve opening (SCG) and pulse arrival
(PPG) is the **pulse transit time (PTT)**. Higher blood pressure stiffens the artery, the pressure
wave travels faster, and PTT shortens.

PTT tracks **change**, not absolute pressure. So the product is not a cuff replacement. A validated
upper-arm cuff establishes a personal baseline at enrolment, and remains the reference for
scheduled readings and for confirmation whenever a trend deviates persistently. What Tera adds is
continuity between clinic visits, on a device the patient already carries.

**The clinical value proposition is portability and record completeness, not "no more cuff".**

### Roles
- **Patient** — runs spot-check sessions, enters cuff readings, logs medication and symptoms.
- **Clinician** — reviews an exception-based summary per monitoring episode.
- **Episode** — a 4–8 week monitoring window opened by a clinic when treatment is adjusted.

---

## 2. Non-negotiable invariants

These are safety and integrity properties, not preferences. Every one of them must be enforced in
code **and** covered by a named test. If a requirement elsewhere in this spec appears to conflict
with one of these, stop and ask.

1. **No mmHg from SCG–PPG, ever.** The `trend_estimate` entity has no systolic or diastolic column
   and no API response derived from SCG–PPG may contain a pressure value. Estimates are expressed
   as a direction (`stable` / `increase` / `decrease`) plus a magnitude in units of the patient's
   own baseline standard deviation. Only `cuff_reading` holds mmHg.
2. **No raw waveform is stored or transmitted.** Camera frames, region-of-interest intensity series,
   and accelerometer sample buffers never leave the handset and are never persisted anywhere. The
   deepest granularity accepted by the API is one derived interval per beat.
3. **Rejected sessions are retained, never discarded.** Status and rejection reason are persisted
   and must be mutually consistent. The clinician summary reports them.
4. **Calibration is versioned and device-bound.** Every estimate references the calibration in force
   at capture time; every calibration references a device profile; at most one calibration is active
   per patient per device. Recalibration inserts a new row and supersedes the old one — it never
   mutates history.
5. **Clinical records are append-only.** No update or delete endpoint on clinical rows. Corrections
   are new rows referencing the original. The audit log is append-only.
6. **The system never diagnoses and never advises on medication.** No endpoint, response field, UI
   string, or generated summary may state or imply a diagnosis, a medication change, or clinical
   reassurance.
7. **Bias toward escalation.** Where signal quality, calibration state, or a symptom report is
   ambiguous, the correct behaviour is to request a cuff reading or clinical contact — never to
   produce an estimate. A false alarm costs a confirmatory measurement; a false reassurance can
   cost much more.
8. **Red-flag symptoms terminate the session.** Chest pain, severe breathlessness, severe headache,
   visual disturbance, or new weakness or speech difficulty produce an immediate instruction to seek
   emergency care, with no measurement offered and no estimate displayed. This path must not depend
   on network availability.
9. **No fabricated data presented as real.** Seeded and synthetic data must be unmistakably labelled
   as such in the API, the UI, and the database. Never invent device benchmark results or clinical
   measurements.
10. **All clinical thresholds are configuration with documented defaults**, never hard-coded magic
    numbers, and every default carries a source comment explaining where it came from.

---

## 3. Repository layout

Monorepo, independent projects, one root.

```
ristek-hackathon/
  CLAUDE.md              <- you create this: invariants + conventions + how to run
  BUILD_SPEC.md          <- this file
  backend/               <- Phase 1: FastAPI + PostgreSQL
  dashboard/             <- Phase 2: web app (clinician + patient views)
  profiler/              <- Phase 3: Flutter device-capability profiler
  docs/
    api.md               <- generated OpenAPI summary
    decisions.md         <- one short entry per non-obvious choice
```

`mobile/` (the full patient capture app) is **out of scope for now** — do not scaffold it.

---

## 4. Phase 1 — Backend  ← start here

**Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL 15+, Pydantic v2, pytest,
`uv` or `pip-tools`, Docker Compose for local Postgres. No Redis unless you can justify it in
`docs/decisions.md`.

### 4.1 Data model

Implement exactly this, as Alembic migrations plus SQLAlchemy models.

| Entity | Notes |
|---|---|
| `patient` | pseudonymous id, clinic id, enrolled_at. No name or contact fields. |
| `monitoring_episode` | patient, start, end, `protocol_params` JSONB: cuff schedule, deviation multiplier `k`, minimum beat count |
| `device_profile` | patient, model, os_version, accel_rate_hz, camera_fps, camera_hw_level, manual_sensor bool, timestamp_source enum, clock_offset_sd_ms, qualified_status enum |
| `calibration` | patient, **device_profile_id**, reference cuff_reading_id, baseline_mean_ms, baseline_sd_ms (>0), n_sessions (>=3), status active/superseded, superseded_by self-FK |
| `measurement_session` | id generated on device (idempotency key), episode, device_profile, calibration (nullable for calibration sessions), model_version, started_at, posture, status, rejection_reason, n_beats_total, n_beats_usable, `ptt_ms REAL[]`, `quality JSONB`, received_at |
| `trend_estimate` | session (unique), calibration, direction enum, magnitude_sd, confidence, computed_at. **No pressure columns.** |
| `cuff_reading` | episode, systolic_mmhg, diastolic_mmhg, pulse_bpm, source enum(manual_entry, photograph), ocr_confidence, taken_at, user_confirmed_at NOT NULL |
| `medication_event`, `symptom_event`, `red_flag_event` | episode, occurred_at, payload JSONB |
| `clinician_summary` | episode, generated_at, delivered_at, viewed_at, contents JSONB |
| `audit_log` | actor, role, action, target, occurred_at. Append-only. |

Constraints that must exist at the database level, not only in application code:

- `CHECK ((status = 'rejected') = (rejection_reason IS NOT NULL))` on `measurement_session`
- `CHECK (baseline_sd_ms > 0)` and `CHECK (n_sessions >= 3)` on `calibration`
- Partial unique index: one `active` calibration per `(patient_id, device_profile_id)`
- Plausibility ranges on `cuff_reading`: systolic 50–300, diastolic 30–200, pulse 25–250
- Bound the length of `ptt_ms` (reject payloads above a configured maximum) so the column cannot be
  abused to smuggle a waveform

### 4.2 API surface

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/device-profiles` | POST | submit profiling result, return eligibility verdict |
| `/v1/sessions/nonce` | POST | issue a single-use nonce, short TTL |
| `/v1/sessions` | POST | submit an accepted or rejected session |
| `/v1/cuff-readings` | POST | submit a user-confirmed cuff reading |
| `/v1/calibrations` | POST | establish or supersede a calibration |
| `/v1/events` | POST | medication, symptom, or red-flag event |
| `/v1/episodes/{id}/timeline` | GET | patient timeline |
| `/v1/episodes/{id}/summary` | GET | clinician exception summary |

Session submission contract:

```
POST /v1/sessions
  Authorization: Bearer <token>
  X-Session-Nonce: <single use>
  Idempotency-Key: <session_id>

201  { "trend": { "direction": "stable", "magnitude_sd": 0.4,
                  "confidence": 0.81, "calibration_id": "..." } }
409  duplicate session_id — return the stored result unchanged
422  payload failed validation
428  nonce absent, expired, or already used
429  rate limit exceeded
```

Timeline responses must return estimates and cuff readings as **distinct types with distinct
field sets**, so a client cannot accidentally render one as the other.

### 4.3 Deviation engine

- Session-level PTT = trimmed mean of usable beats (discard beyond 1.5 × IQR).
- Baseline = mean and standard deviation of session-level PTT across at least three accepted
  calibration sessions.
- `possible_deviation` when `|session_ptt − baseline_mean| >= k × baseline_sd`, default `k = 2`,
  configurable per episode.
- `persistent` when a repeat session within the configured window also deviates. A single deviating
  session never triggers a cuff request.
- Direction: shorter PTT → `increase`. Document the physiological reason in a comment.
- Confidence derives from beat count and quality metrics. Do not invent a formula that implies
  clinical accuracy — keep it simple, documented, and clearly a heuristic.
- Output is never mmHg.

### 4.4 Payload plausibility (defence in depth)

The quality gate runs on the device. The backend independently rejects implausible payloads with
422 rather than trusting the client: PTT values outside 80–400 ms, `n_usable > n_detected`,
`n_usable` below the configured minimum for a `completed` session, achieved rates below the
device profile's qualified band, missing quality fields, `ptt_ms` longer than the configured bound.

### 4.5 Security

- OAuth2 with short-lived JWT access tokens plus refresh; role claims `patient`, `clinician`,
  `admin`. Clinician access scoped to episodes where they are the reviewing professional.
- Single-use nonce with TTL for session ingest; reuse returns 428.
- Idempotency on `session_id`.
- Per-token and per-patient rate limits on ingest and summary endpoints.
- No secrets in the repo. `.env.example` only.
- Structured logs with **no clinical content**: session id, device profile id, model version, rates,
  gate outcome, timings. Never pressure values, PTT values, symptom text, or medication detail.

### 4.6 Seed data and replay harness

Two CLI commands, both essential.

`seed-demo` builds one realistic four-week episode for the proposal's persona: a 52-year-old with
recently intensified treatment. It must include three calibration sessions, roughly thirty routine
sessions with a plausible slow PTT drift and day-to-day scatter, several rejected sessions across
different rejection reasons, a handful of cuff readings, medication events, one symptom event, one
deviation → repeat → cuff-confirmation sequence, and one recalibration so the versioning is
exercised. Every seeded row must carry `synthetic: true` and the API must surface that flag.

`replay <file.json>` posts a recorded or synthetic session through the real API as if it came from a
device, including nonce acquisition and idempotency. This is the demo fallback for when no phone is
available or the venue network is hostile — it must exercise the same code path as a real device, not
a shortcut.

### 4.7 Tests

pytest, with these named explicitly so a reviewer can find them:

- `test_trend_estimate_has_no_pressure_column` — introspect the schema and assert absence
- `test_rejected_session_requires_reason` and the converse
- `test_only_one_active_calibration_per_patient_per_device`
- `test_recalibration_supersedes_and_does_not_mutate`
- `test_estimate_references_calibration_in_force_at_capture_time`
- `test_duplicate_session_id_returns_stored_result`
- `test_nonce_cannot_be_reused`
- `test_implausible_ptt_rejected_with_422`
- `test_ptt_array_length_bound_enforced`
- `test_single_deviating_session_does_not_request_cuff`
- `test_persistent_deviation_requests_cuff`
- `test_clinical_rows_have_no_update_or_delete_route`
- `test_logs_contain_no_clinical_values`
- `test_timeline_returns_estimates_and_readings_as_distinct_types`

Target: every invariant in section 2 maps to at least one test.

### 4.8 Deliverables

Docker Compose that brings up Postgres and the API, migrations that run clean from empty,
`seed-demo` producing a browsable episode, all tests green, `docs/api.md` generated from OpenAPI,
and a `README.md` with exact run commands.

**Stop at this point, run the full test suite, and report before starting Phase 2.**

---

## 5. Phase 2 — Dashboard

**Stack:** Next.js (App Router) + TypeScript + Tailwind. Server components where sensible. No
component library unless it earns its place in `docs/decisions.md`. Consumes the Phase 1 API — no
mock data layer; run against the seeded backend.

### 5.1 Palette

```
--ink        #12304A   darkest navy — primary text, sidebar, headers
--brand      #114B5F   dark teal   — primary actions, confirmed-reading emphasis
--muted      #456990   slate blue  — secondary text, borders, estimate treatment
--surface    #E4FDE1   pale mint   — page background tint, cards
```

Define these as CSS custom properties and Tailwind theme tokens. Use no colour outside this palette
plus white and a neutral grey ramp derived from `--ink`.

**One hard rule:** no colour may imply clinical reassurance. Pale mint is a background tint, not a
"good result" signal. Do not introduce green-for-good or red-for-bad status colours for blood
pressure. Reserve any warning treatment for *system* states — a rejected session, a stale
calibration, an unsynchronised device — never for a physiological value.

### 5.2 Visual separation of record types

This is invariant 1 expressed in the interface, and it is the most important design requirement in
this phase. The three record types must be unmistakable at a glance, without reading labels:

| Type | Treatment |
|---|---|
| **Cuff-confirmed reading** | Solid `--brand` fill, white text, large numerals `148/92`, unit shown, badge "CONFIRMED — UPPER-ARM CUFF" |
| **Trend estimate** | Outlined card, `--muted` 1px border, no fill, **no numerals at all** — a direction arrow plus wording such as "within your usual range" or "higher than your usual range", a confidence indicator, and a badge "ESTIMATE — NOT A BLOOD-PRESSURE READING" |
| **Rejected session** | Dashed `--muted` border, reduced opacity, reason text, and a retry affordance. Present, visible, never hidden |

If a design decision would make an estimate look like a measurement, the design is wrong.

### 5.3 Screens

1. **Clinician — episode summary.** Exception-based: cuff-confirmed readings, notable changes,
   rejected sessions with reasons, reported symptoms, red-flag events, medication adherence,
   calibration status and version history. Designed to be scanned in under two minutes, because the
   evidence says clinicians have very little consultation time.
2. **Clinician — episode list.** Active episodes with a small set of at-a-glance system indicators:
   session yield, days since last cuff reading, unsynchronised sessions, calibration staleness.
3. **Patient — timeline.** Chronological mix of the three record types plus medication and symptom
   events, using the treatments above.
4. **Patient — session detail.** Quality metrics in plain language, what the gate checked, and what
   to do next.
5. **Device profile / eligibility.** Renders a `device_profile` record and its verdict, with the
   measured numbers and what each one means.

### 5.4 Non-negotiables in this phase

- Never render a `magnitude_sd` as though it were mmHg.
- Every estimate carries its "not a blood-pressure reading" badge; it is not dismissible.
- Show the seeded/synthetic flag prominently wherever synthetic data appears.
- Accessible contrast throughout; check `--muted` on `--surface` and darken locally if it fails.
- Keyboard navigable, sensible focus states.

---

## 6. Phase 3 — Device capability profiler

A small standalone Flutter app whose **only** job is to answer: can this handset run Tera? Its
output fills the device eligibility table in the proposal and decides which phones are used for the
demo. It is not the patient app and must not grow into one.

**Stack:** Flutter, Android only, minSdk 26. Camera and sensor introspection require a Kotlin
`MethodChannel` — the standard camera plugin does not expose what is needed here.

### 6.1 Measurements

1. **Accelerometer achieved rate.** Register at the fastest available reporting period with batching
   disabled (max report latency zero). Collect 60 s. Report mean rate computed from sample
   timestamps, standard deviation of inter-sample interval, and dropped-sample estimate. Do not
   trust the requested rate — measure it.
2. **Elevated-rate permission.** Declare `HIGH_SAMPLING_RATE_SENSORS` in the manifest. Report whether
   rates above 200 Hz are actually delivered, since without the declaration the platform caps at
   200 Hz on Android 12+.
3. **Camera characteristics** via `MethodChannel`: `INFO_SUPPORTED_HARDWARE_LEVEL`, whether
   `REQUEST_AVAILABLE_CAPABILITIES` contains `MANUAL_SENSOR`, `SENSOR_INFO_TIMESTAMP_SOURCE`, the
   available YUV output sizes, and the minimum frame duration for the smallest usable size.
4. **Camera sustained frame rate.** 60 s capture at the smallest adequate YUV size with torch on and
   auto-exposure, auto-white-balance, and auto-focus all locked. Report achieved fps from per-frame
   hardware timestamps, dropped-frame percentage, and 99th-percentile inter-frame interval. Then
   repeat immediately so the warm-device result is captured separately — thermal throttling is
   exactly what we are looking for.
5. **Thermal and battery.** Thermal status before and after each run; battery level and whether
   charging.
6. **Clock offset and stability.** Read the realtime and uptime clocks back to back, derive the
   offset, and repeat across three separate runs so the spread can be reported. Note in the UI that
   what matters is stability, not the absolute value, because a constant offset is absorbed by
   personal calibration.
7. **Per-frame processing time.** Time the region-of-interest mean computation per frame and report
   mean and 99th percentile, to verify the real-time budget on actual hardware.

### 6.2 Output

One screen with a Run button and a live progress log, then a results view. Export as JSON and as a
copy-paste-ready markdown table row, so results from several phones can be pasted straight into the
proposal. Optionally POST to `/v1/device-profiles` if a backend URL is configured.

**Report measured values only.** If a measurement fails, say so — never substitute an estimate or a
plausible-looking number.

---

## 7. Working conventions

- Small, focused commits. Conventional commit messages. Run tests before each commit.
- One short entry in `docs/decisions.md` for every non-obvious choice, including anything you
  deviate from in this spec and why.
- Type hints and docstrings on public functions. Comment the *why*, not the *what*.
- No TODO comments without a corresponding entry in `docs/decisions.md`.
- If a requirement here is ambiguous or conflicts with an invariant, stop and ask rather than
  guessing. Guessing about clinical behaviour is the one failure mode with real-world cost.
- Do not add dependencies casually; each one needs a one-line justification.

## 8. Out of scope

Do not build: the patient capture app, on-device signal processing, mmHg estimation from SCG–PPG,
diagnosis or triage logic beyond the red-flag routing described here, seven-segment OCR (manual entry
only for now), notification delivery, or any clinical decision support.