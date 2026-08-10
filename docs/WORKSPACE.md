# CLAUDE.md — Tera working root

This is the folder above the three repos, not a repo itself. Read this before entering any of
them; it exists so you never have to re-read `proposal.pdf` to start work.

The canonical copy is committed at `tera-backend/docs/WORKSPACE.md`. If this root file is ever
lost again: `cp tera-backend/docs/WORKSPACE.md ./CLAUDE.md`.

## What Tera is

Tera is a hybrid, cuff-referenced home blood-pressure *monitoring* system: a phone records
seismocardiography (accelerometer on the sternum) and photoplethysmography (rear camera on a
fingertip, torch on), and the gap between the two — pulse transit time — tracks how arterial
stiffness is changing. PTT tracks *change*, never absolute pressure; a validated upper-arm cuff
sets the personal baseline and remains the only source of a mmHg number. The value proposition is
portability and record completeness, not "no more cuff".

## The three repos

| Repo | Contains | Detailed authority |
|---|---|---|
| `tera-backend/` | FastAPI + SQLAlchemy 2 + Alembic + Postgres, `docker-compose.yml`, seed/replay/docs CLIs. Also the docs home: `docs/api.md`, `docs/proposal.pdf`. | `BUILD_SPEC.md`, `docs/decisions.md` |
| `tera-web/` | `dashboard/` — Next.js clinician summary, patient timeline, episode list, login. Design system and `@theme` tokens live here. | `BUILD_SPEC.md`, `docs/decisions.md` |
| `tera-mobile/` | `patient/` (the capture app), `profiler/` (device-capability harness), `packages/tera_capture` (acquisition layer, Dart + Kotlin, no UI dependency). | `BUILD_SPEC.md`, `docs/decisions.md` |

Each repo also carries its own `CLAUDE.md`. Those are the per-repo detail; this file is the
orientation above them.

**`decisions.md` has diverged.** All three began as copies of one file — 67 entries are common —
and each has since grown its own. A decision recorded in `tera-web` is not visible from
`tera-mobile`. When a decision spans repos, check the other two before assuming it is unrecorded.

## Authority split

- **`proposal.pdf`** (in `tera-backend/docs/`) — product scope, clinical constraints, device
  thresholds, success metrics. The judge-facing promise.
- **`BUILD_SPEC.md`** (identical in all three repos) — API surface, payloads, schema.
- **`docs/decisions.md`** — every decision already taken, and why. Read before re-opening one.

If those three disagree, the invariants in the per-repo `CLAUDE.md` win, and you stop and ask.

## Standing constraints

These are rules, not preferences.

1. Estimates and cuff-confirmed readings never share a visual language. `trend_estimate` has no
   mmHg field, in the schema or anywhere downstream. A cuff reading is a solid fill with large
   numerals; an estimate is an outline with no numerals in the value area.
2. No waveform is persisted on the clinical path. The deepest granularity the API accepts is one
   derived interval per beat. The developer CSV export is a compile-time-gated local exception,
   never reachable from the submit path.
3. Clinical thresholds come from the environment via pydantic-settings, never literals in logic,
   and every default carries a source comment.
4. No emoji. Anywhere — code, UI, docs, commit messages.
5. Palette: `#114B5F` brand, `#456990` baltic, `#E4FDE1` mint, `#12304A` ink, `#6B2737` plum.
   Plum is reserved for system state only; physiological state is differentiated by *form*, never
   hue. Neutrals are derived from `#12304A`. All colour goes through `@theme` tokens — no raw hex
   in components.

## Working discipline

- Decide routine matters yourself and record them in the right `decisions.md`. Stop only for a
  safety question or a conflict with an invariant.
- Incremental commits with descriptive messages. Tests green before each one.
- Never commit secrets. `.env` is git-ignored; only `.env.example` is tracked.
- Update `docs/decisions.md` and `docs/api.md` as you go, not at the end.
- Hand over at a clean commit boundary.

## Competition dates (2026)

- **10–13 August** — Innovation Week, online: mentoring plus development.
- **13 August, 23:55 WIB** — final product submission. Hard deadline.
- **14 August** — offline exhibition and pitching, Fasilkom UI.

## Running everything

**Backend.** From `tera-backend/`:

```bash
docker compose up -d --build          # Postgres on host :5434, API on :8000; migrations auto-run
curl http://localhost:8000/health     # {"status":"ok"}
```

Compose runs migrations but **not** the seed. The demo data lives in the `tera_pgdata` volume and
was seeded from the host. To recreate it:

```bash
cd backend && source .venv/Scripts/activate   # or .venv/Scripts/activate on cmd
tera-seed-demo                                # one 4-week synthetic episode, all rows flagged synthetic
pytest                                        # 247 tests; pytest -m invariant for the 159 invariant ones
```

**Dashboard.** From `tera-web/dashboard/` (needs the backend up and seeded):

```bash
cp .env.example .env.local   # fill the demo passwords from tera-backend/backend/.env
npm install && npm run dev   # http://localhost:3000
npx tsc --noEmit && npx eslint . && npx next build
node scripts/screenshots.mjs # logs in for real, shoots 4 pages x desktop/mobile into screenshots/
```

**Patient app.** From `tera-mobile/patient/` (Android only, minSdk 26):

```bash
flutter test                 # 87 tests, no device
flutter build apk --release \
  --dart-define=TERA_API_URL=http://<laptop-lan-ip>:8000 \
  --dart-define=TERA_DEBUG_CAPTURE=false
```

Verified 9 August: 45.6 MB APK in about four minutes, `CAMERA` and `HIGH_SAMPLING_RATE_SENSORS`
confirmed in the merged release manifest. Signed with the debug keystore — the Flutter template's
TODO is still in `android/app/build.gradle.kts`.

**Putting it on a physical phone: `tera-mobile/HARDWARE_CHECKLIST.md`.** Ordered, with what each
failure looks like. Read it before the day, not on it.

Both defines matter:

- `TERA_API_URL` — without it the app falls back to `10.0.2.2:8000`, which is the host *as seen
  from an Android emulator* and is unreachable from a real handset. On real hardware this must be
  the laptop's LAN address or every request fails at sign-in.
- `TERA_DEBUG_CAPTURE` — `true` compiles in the raw accelerometer/ROI CSV export for developing
  the signal chain against real data. It must be `false` (or absent) for anything a judge or a
  patient touches: the flag is compile-time precisely so an unflagged build cannot reach that code
  at all.

Screenshots from an attached device or emulator: `.\tool\screenshots.ps1` (writes into
`patient/screenshots/`, git-ignored). It takes one shot; tap to the next screen yourself.

`profiler/` and `packages/tera_capture/` build and test the same way (`flutter test` — 21 and 4
tests respectively).
