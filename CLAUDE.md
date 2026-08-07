# CLAUDE.md — Tera

Read this before touching anything in this repo. `BUILD_SPEC.md` is the full spec; this file is the
part that must survive across sessions.

**Tera** is a hybrid, cuff-referenced home blood-pressure *monitoring* system. A phone captures
seismocardiography (accelerometer on the sternum) and photoplethysmography (rear camera, fingertip,
torch on). The gap between aortic valve opening and pulse arrival is the **pulse transit time
(PTT)**. Higher pressure → stiffer artery → faster wave → shorter PTT.

PTT tracks **change**, not absolute pressure. A validated upper-arm cuff sets the personal baseline
and stays the reference. **The value proposition is portability and record completeness, not "no
more cuff".**

---

## 1. The ten non-negotiable invariants

Safety and integrity properties, not preferences. Each is enforced in code **and** covered by a
named test. If a new requirement appears to conflict with one of these, **stop and ask** — do not
reconcile it yourself.

1. **No mmHg from SCG–PPG, ever.** `trend_estimate` has no systolic/diastolic column, and no API
   response derived from SCG–PPG may contain a pressure value. Estimates are a direction
   (`stable` / `increase` / `decrease`) plus a magnitude in units of the patient's own baseline
   standard deviation. Only `cuff_reading` holds mmHg.
2. **No raw waveform is stored or transmitted.** Camera frames, ROI intensity series and
   accelerometer sample buffers never leave the handset and are never persisted. The deepest
   granularity the API accepts is **one derived interval per beat**.
3. **Rejected sessions are retained, never discarded.** Status and rejection reason are persisted
   and must be mutually consistent. The clinician summary reports them.
4. **Calibration is versioned and device-bound.** Every estimate references the calibration in force
   *at capture time*; every calibration references a device profile; at most one calibration is
   active per patient per device. Recalibration inserts a new row and supersedes the old one — it
   never mutates history.
5. **Clinical records are append-only.** No update or delete endpoint on clinical rows. Corrections
   are new rows referencing the original. The audit log is append-only.
6. **The system never diagnoses and never advises on medication.** No endpoint, response field, UI
   string or generated summary may state or imply a diagnosis, a medication change, or clinical
   reassurance.
7. **Bias toward escalation.** Where signal quality, calibration state or a symptom report is
   ambiguous, request a cuff reading or clinical contact — never produce an estimate. A false alarm
   costs a confirmatory measurement; a false reassurance can cost much more.
8. **Red-flag symptoms terminate the session.** Chest pain, severe breathlessness, severe headache,
   visual disturbance, or new weakness or speech difficulty produce an immediate instruction to seek
   emergency care, with no measurement offered and no estimate displayed. **This path must not
   depend on network availability** — the handset shows it locally; the API call is a record, not a
   precondition.
9. **No fabricated data presented as real.** Seeded and synthetic data is unmistakably labelled in
   the API, the UI and the database (`synthetic` boolean on every clinical table, surfaced in every
   response). Never invent device benchmark results or clinical measurements.
10. **All clinical thresholds are configuration with documented defaults**, never hard-coded magic
    numbers, and every default carries a source comment explaining where it came from. They live in
    `backend/app/config.py` and per-episode `monitoring_episode.protocol_params`.

### Where each invariant is enforced and tested

| # | Enforced in | Named test |
|---|---|---|
| 1 | schema has no pressure column on `trend_estimate`; `TrendEstimateOut` schema | `test_trend_estimate_has_no_pressure_column`, `test_no_pressure_value_in_any_estimate_response` |
| 2 | `ptt_ms` length bound + plausibility gate | `test_ptt_array_length_bound_enforced`, `test_no_raw_waveform_fields_accepted` |
| 3 | DB `CHECK ((status='rejected') = (rejection_reason IS NOT NULL))` | `test_rejected_session_requires_reason`, `test_accepted_session_must_not_have_reason`, `test_rejected_sessions_appear_in_summary` |
| 4 | partial unique index + `superseded_by` + append-only trigger + capture-time resolution | `test_only_one_active_calibration_per_patient_per_device`, `test_recalibration_supersedes_and_does_not_mutate`, `test_estimate_references_calibration_in_force_at_capture_time` |
| 5 | no PUT/PATCH/DELETE routes; append-only trigger on every clinical table | `test_clinical_rows_have_no_update_or_delete_route`, `test_clinical_tables_reject_update_and_delete` (real UPDATE + DELETE per table), `test_audit_log_is_append_only` |
| 6 | vocabulary guard over responses and summary contents | `test_no_diagnostic_or_medication_advice_language` |
| 7 | estimate withheld when calibration/quality ambiguous | `test_missing_calibration_yields_no_estimate_and_requests_cuff`, `test_single_deviating_session_does_not_request_cuff`, `test_persistent_deviation_requests_cuff` |
| 8 | `/v1/events` red-flag response; no estimate returned | `test_red_flag_event_returns_emergency_instruction_and_no_estimate` |
| 9 | `synthetic` column on every clinical table, surfaced in API | `test_seeded_rows_are_flagged_synthetic_everywhere` |
| 10 | `app/config.py` — no literal thresholds in logic | `test_thresholds_come_from_config_not_literals` |

---

## 2. Repo layout

```
ristek-hackathon/
  CLAUDE.md            <- this file
  BUILD_SPEC.md        <- the spec
  docker-compose.yml   <- Postgres + API
  backend/             <- Phase 1: FastAPI + SQLAlchemy 2 + Alembic + Postgres  [DONE]
  dashboard/           <- Phase 2: Next.js clinician + patient views            [PARTIAL]
  profiler/            <- Phase 3: Flutter device-capability profiler           [BUILDS, UNTESTED ON HW]
  packages/
    tera_capture/      <- acquisition layer: Dart + Kotlin, no UI dependency
  docs/
    api.md             <- generated from OpenAPI (`tera-docs` CLI)
    decisions.md       <- one short entry per non-obvious choice
```

`mobile/` (the full patient capture app) is **out of scope** — do not scaffold it.

```
backend/
  app/
    config.py          <- ALL clinical thresholds, each with a source comment
    models/            <- SQLAlchemy 2.x declarative models
    schemas/           <- Pydantic v2 request/response models
    api/v1/            <- routers, one module per resource
    services/          <- deviation engine, plausibility gate, calibration, summary, audit
    security/          <- JWT, password hashing, nonce store, rate limiter
    cli/               <- seed_demo, replay, docs generation
  alembic/versions/    <- migrations
  tests/
```

## 3. Run commands

All commands run from the repo root unless stated. Copy `backend/.env.example` to `backend/.env`
first; it is the only place secrets live and it is git-ignored.

```bash
# bring up Postgres + API (migrations run automatically on API start)
docker compose up -d --build
docker compose logs -f api

# API docs: http://localhost:8000/docs    health: http://localhost:8000/health
# Postgres is published on host port 5434 — 5432 and 5433 were already taken on the
# development machine. Change docker-compose.yml and backend/.env together if you move it.
```

Local development against the Compose Postgres:

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate      # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"

alembic upgrade head                # migrations from empty
tera-seed-demo                      # one browsable 4-week synthetic episode
uvicorn app.main:app --reload       # http://localhost:8000

tera-replay samples/session_normal.json --base-url http://localhost:8000
tera-docs                           # regenerate docs/api.md from the live OpenAPI schema
```

Tests (needs a real Postgres — arrays, JSONB, partial indexes and triggers are all exercised):

```bash
cd backend
pytest                              # full suite: 162 tests
pytest -m invariant                 # the invariant subset: 91 tests
```

`TERA_DATABASE_URL` points at the app database; `TERA_TEST_DATABASE_URL` at the test one. The test
fixture creates and drops its own database per run, so tests never touch dev data.

Dashboard (Phase 2 — needs the backend running and seeded):

```bash
cd dashboard
cp .env.example .env.local      # fill in the demo passwords from backend/.env
npm install
npm run dev                     # http://localhost:3000
npx tsc --noEmit && npx eslint . && npx next build
```

Built so far: the palette and design system, the clinician episode summary
(`/clinician/[episodeId]`) and the patient timeline (`/patient/[episodeId]`). The episode list,
session detail and device-profile screens are not built yet.

**Before changing anything in `dashboard/components/RecordCards.tsx`, read BUILD_SPEC 5.2.** That
file is invariant 1 expressed in the interface: a cuff reading is a solid fill with large
numerals, an estimate is an outline with no numerals in the value area, a rejected session is
dashed and faded. If a change would make an estimate look more like a measurement, the change is
wrong. `magnitude_sd` must not be rendered in the patient view at all.

Profiler and capture layer (Phase 3 — Android only, minSdk 26):

```bash
cd profiler
flutter pub get
flutter test                    # 21 tests, no device needed
flutter build apk --release     # build/app/outputs/flutter-apk/app-release.apk
adb install -r build/app/outputs/flutter-apk/app-release.apk
```

The profiler has a **smoke mode** (20 s, 5 s per stage) for debugging HAL behaviour. It returns
a `SmokeReport`, a separate type from `ProfileResult` with no route to a markdown row or the
upload — five seconds is not a sustained-rate measurement, and the guarantee is structural
rather than a flag someone must remember to check.

It also **verifies the clock basis** rather than trusting `SENSOR_INFO_TIMESTAMP_SOURCE`. A
handset declaring `REALTIME` while timestamping in uptime looks normal on every other measure
and invalidates every cross-stream figure. See `packages/tera_capture/lib/src/clock_basis.dart`.

**`packages/tera_capture` is the acquisition layer of the patient app, shipped first.** It has no
dependency on any UI and must not gain one. It deliberately does **not** do buffer retention,
filtering, event detection, beat pairing, or the quality gate — that boundary is written out in
full in `docs/decisions.md` and is what the patient capture app will build on. If any of those
five appears in the package, the boundary has moved.

**The capture paths have never run on real hardware.** The project builds and the statistics are
unit-tested, but no Android device was available. See `profiler/README.md` for what to check
first.

## 4. Conventions

- Small, focused commits, conventional commit messages, tests green before each commit.
- One entry in `docs/decisions.md` for **every** non-obvious choice and every deviation from
  `BUILD_SPEC.md`, with the reason.
- No TODO comment without a matching `docs/decisions.md` entry.
- Type hints and docstrings on public functions. Comment the **why**, not the what.
- Every new dependency needs a one-line justification in `docs/decisions.md`.
- Clinical thresholds go in `config.py` with a source comment. Never inline a number in logic.
- Structured logs carry **no clinical content**: ids, versions, rates, gate outcome, timings only.
  Never pressure values, PTT values, symptom text or medication detail. `app/logging_config.py`
  enforces a deny-list; add to it rather than around it. It scrubs three ways — `extra=` fields by
  key name, free text by `key: value` pattern, and exception messages by dropping them entirely
  (a database error carries the whole failing row, twice over). `tests/test_error_path_leakage.py`
  fires clinically-loaded payloads at every endpoint and forces an unhandled exception.
- An unhandled exception returns an opaque `incident_id` and nothing else. To investigate, grep
  the logs for that id; the logged frame list says where to look.
- When in doubt about clinical behaviour, escalate to a cuff reading. Guessing here is the one
  failure mode with real-world cost.

## 5. Where the spec conflicted with an invariant

Section 2 says an invariant wins. Two conflicts arose; both are argued in full in
`docs/decisions.md`. Do not "fix" either of these back without re-reading that argument.

- **The achieved-rate check gates completed sessions only.** §4.4 lists "achieved rates below the
  device profile's qualified band" as a 422 with no status qualifier — but a session rejected for
  `sensor_rate_below_qualified` reports low rates *because that is why it failed*, and 422 would
  discard it. Invariant 3 wins. Every structural check still applies to every payload.
- **Supersession writes to the old calibration row.** Invariants 4 and 5 say history is never
  mutated, while invariant 4 also specifies `status` and a `superseded_by` self-FK. Read as: the
  baseline is immutable, the supersession pointer is not. A trigger enforces exactly that line and
  makes supersession one-way.

## 6. Deviations from BUILD_SPEC.md

Recorded in full in `docs/decisions.md`. The structural ones worth knowing before you read the code:

- `app_user` table and `monitoring_episode.reviewing_clinician_id` were added — the spec requires
  clinician access "scoped to episodes where they are the reviewing professional" but lists no user
  entity and no such column.
- `calibration` gained `established_at` / `superseded_at`, and a `calibration_source_session` join
  table — needed to resolve "the calibration in force at capture time" (invariant 4) and to let the
  server compute `n_sessions` and the baseline itself rather than trusting the client.
- `synthetic BOOLEAN NOT NULL DEFAULT false` on every clinical table (invariant 9).
- `POST /v1/auth/token` and `/v1/auth/refresh` were added; the spec mandates OAuth2 but omits them
  from the endpoint table.
- `cuff_reading.source = 'photograph'` exists in the enum but the API rejects it with 422 — OCR is
  explicitly out of scope (spec §8), and accepting the value would imply a capability we do not have.
