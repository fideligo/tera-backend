# Tera — backend

Part of **Tera**, a hybrid, cuff-referenced home blood-pressure *monitoring* system. This
repository holds the API, the data model and the deviation engine.

| Repository | Contents |
|---|---|
| **tera-backend** (this one) | FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL |
| [tera-mobile](https://github.com/fideligo/tera-mobile) | Device capability profiler and the `tera_capture` acquisition layer (Flutter + Kotlin) |
| [tera-web](https://github.com/fideligo/tera-web) | Clinician and patient views (Next.js) |

---

## What this system is

A smartphone records seismocardiography from the accelerometer on the sternum and
photoplethysmography from the rear camera with a fingertip on the lens. The interval between
them is the **pulse transit time (PTT)**, which tracks *change* in blood pressure, not its
absolute value.

**Tera is not a cuff replacement.** A validated upper-arm cuff sets the personal baseline and
remains the reference. What Tera adds is continuity between clinic visits.

The system does not diagnose, does not advise on medication, and does not offer clinical
reassurance. The ten invariants that make that true in code are in [`CLAUDE.md`](CLAUDE.md);
each maps to at least one named test.

The most consequential one, worth stating here: **no API response derived from SCG–PPG contains
a blood-pressure value.** `trend_estimate` has no systolic or diastolic column, and a schema
introspection test enforces it against the live database. Only `cuff_reading` holds mmHg.

## Quick start

Requires Docker.

```bash
cp backend/.env.example backend/.env
# generate a secret: python -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose up -d --build
docker compose exec api tera-seed-demo
```

- API docs: <http://localhost:8000/docs> · health: <http://localhost:8000/health>
- Postgres on host port **5434** (5432/5433 are commonly taken by other local instances)

Migrations run from empty on API start.

## Local development

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -e ".[dev]"

docker compose up -d db
alembic upgrade head
tera-seed-demo
uvicorn app.main:app --reload
```

| Command | What it does |
|---|---|
| `alembic upgrade head` | Schema from empty, including the append-only triggers |
| `tera-seed-demo` | One synthetic four-week episode (`--rejection-rate` to vary yield) |
| `tera-replay <file.json>` | Post a session through the real API, exactly as a device would |
| `tera-docs` | Regenerate [`docs/api.md`](docs/api.md) from the live OpenAPI schema |
| `pytest` | 175 tests |
| `pytest -m invariant` | The 91 tests that map to a section-2 invariant |

Tests run against a **real PostgreSQL database**, not SQLite — the invariants are enforced by
native arrays, JSONB, a partial unique index, CHECK constraints and PL/pgSQL triggers, and a
test against a database without those would prove nothing. The fixture creates and drops its own
database, so tests never touch dev data.

## Reading the code

| Path | What lives there |
|---|---|
| `backend/app/config.py` | Every clinical threshold, each with a source comment. Nothing else may inline a number. |
| `backend/alembic/versions/0001_initial_schema.py` | Schema, CHECK constraints, partial unique index, append-only triggers |
| `backend/app/services/deviation.py` | The deviation engine. Pure functions, no database. |
| `backend/app/services/plausibility.py` | Server-side payload gate — the backend does not trust the client |
| `backend/app/services/language.py` | Every user-facing string, in one place, checked against a deny-list |
| `backend/app/schemas/timeline.py` | Why an estimate cannot be rendered as a measurement |

[`docs/decisions.md`](docs/decisions.md) records every non-obvious choice, every deviation from
[`BUILD_SPEC.md`](BUILD_SPEC.md), and the two places where the spec conflicted with an invariant
and the invariant won.

## Configuration

`backend/.env.example` lists everything. No secrets are committed; `.env` is git-ignored.
Startup refuses to run with the default JWT secret when `TERA_ENV=production`.
