"""Test fixtures.

The suite runs against a **real PostgreSQL database**, not SQLite. It has to: the invariants are
enforced by native arrays, JSONB, a partial unique index, CHECK constraints and PL/pgSQL
triggers, and a test that ran against a database without those would prove nothing about the
system that ships.

The test database is created and dropped by the session fixture, so it never touches dev data.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

# Point the application at the test database *before* anything imports the engine.
from app.config import Settings, get_settings, reset_settings_cache

_bootstrap_settings = Settings()
os.environ["TERA_DATABASE_URL"] = _bootstrap_settings.test_database_url
os.environ["TERA_ENV"] = "test"
reset_settings_cache()

from app import db as app_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AppUser,
    CameraHardwareLevel,
    CuffReading,
    CuffSource,
    DeviceProfile,
    MonitoringEpisode,
    Patient,
    Posture,
    QualifiedStatus,
    SessionStatus,
    TimestampSource,
    UserRole,
)
from app.security.passwords import hash_password  # noqa: E402
from app.security.ratelimit import limiter  # noqa: E402

#: Truncated between tests. Order does not matter with CASCADE, but listing them explicitly
#: means a newly added table fails loudly here rather than leaking rows between tests.
ALL_TABLES = (
    "calibration_source_session", "trend_estimate", "measurement_session", "clinician_summary",
    "medication_event", "symptom_event", "red_flag_event", "calibration", "cuff_reading",
    "monitoring_episode", "device_profile", "refresh_token", "app_user", "patient",
    "session_nonce", "audit_log",
    # Auth rate-limit counters now live in Postgres, so they persist across tests unless they are
    # truncated here. One test's failed logins would otherwise exhaust another test's allowance.
    "rate_limit_counter",
)

DEMO_PASSWORD = "test-password-not-a-secret"


@pytest.fixture
def settings():
    """Process-wide settings, for tests that decode a token or read a threshold themselves."""
    return get_settings()


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> Iterator[None]:
    """Create the test database, migrate it from empty, and drop it afterwards."""
    settings = get_settings()
    url = sa.make_url(settings.database_url)
    db_name = url.database
    admin_url = url.set(database="postgres")

    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{db_name}"'))
    admin_engine.dispose()

    app_db.reset_engine_cache()
    _run_migrations()

    yield

    app_db.reset_engine_cache()
    admin_engine = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    admin_engine.dispose()


def _run_migrations() -> None:
    """Run ``alembic upgrade head`` against the test database.

    Migrations rather than ``metadata.create_all``: the triggers and the deferrable FK only
    exist in the migration, and those are precisely what several invariant tests assert.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    """Truncate every table between tests.

    TRUNCATE rather than DELETE because the append-only triggers block row deletion by design
    (invariant 5) — which is itself covered by a test.
    """
    limiter.reset()
    engine = app_db.get_engine()
    with engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} CASCADE"))
    yield


@pytest.fixture
def db() -> Iterator[Session]:
    """A database session for direct assertions."""
    session = app_db.get_sessionmaker()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    """An HTTP client bound to the app."""
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- domain fixtures


@pytest.fixture
def patient(db: Session) -> Patient:
    row = Patient(
        pseudonym=f"TERA-TEST-{uuid.uuid4().hex[:8]}",
        clinic_id="CLINIC-TEST",
        enrolled_at=datetime.now(tz=timezone.utc) - timedelta(days=30),
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def clinician(db: Session) -> AppUser:
    row = AppUser(
        subject=f"clinician-{uuid.uuid4().hex[:8]}@test.invalid",
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.CLINICIAN,
        clinic_id="CLINIC-TEST",
        patient_id=None,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def patient_user(db: Session, patient: Patient) -> AppUser:
    row = AppUser(
        subject=f"patient-{uuid.uuid4().hex[:8]}@test.invalid",
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.PATIENT,
        clinic_id="CLINIC-TEST",
        patient_id=patient.id,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def episode_with_pinned_beat_floor(
    db: Session, patient: Patient, clinician: AppUser
) -> MonitoringEpisode:
    """An episode that overrides the beat floor, for testing that the override still wins."""
    row = MonitoringEpisode(
        patient_id=patient.id,
        reviewing_clinician_id=clinician.id,
        started_at=datetime.now(tz=timezone.utc) - timedelta(days=28),
        protocol_params={"deviation_k": 2, "min_beat_count": 30, "persistence_window_hours": 48},
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def episode(db: Session, patient: Patient, clinician: AppUser) -> MonitoringEpisode:
    row = MonitoringEpisode(
        patient_id=patient.id,
        reviewing_clinician_id=clinician.id,
        started_at=datetime.now(tz=timezone.utc) - timedelta(days=28),
        # **No `min_beat_count` here, deliberately.** It used to pin 30, which meant every ingest
        # test ran against a hard-coded floor while the config default moved underneath them — so
        # lowering `min_usable_beats` to 12 passed the whole suite and still 422'd a real capture
        # with 17 beats. A fixture that restates a default is a fixture that hides a change to it.
        #
        # The override path is exercised by `episode_with_pinned_beat_floor` below, which is where
        # a per-episode value belongs.
        protocol_params={"deviation_k": 2, "persistence_window_hours": 48},
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def device_profile(db: Session, patient: Patient) -> DeviceProfile:
    row = DeviceProfile(
        patient_id=patient.id,
        model="Test Handset",
        os_version="Android 14",
        accel_rate_hz=200.0,
        camera_fps=60.0,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.0,
        qualified_status=QualifiedStatus.QUALIFIED,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def cuff_reading(db: Session, episode: MonitoringEpisode) -> CuffReading:
    taken_at = datetime.now(tz=timezone.utc) - timedelta(days=28)
    row = CuffReading(
        episode_id=episode.id,
        systolic_mmhg=152,
        diastolic_mmhg=94,
        pulse_bpm=76,
        source=CuffSource.MANUAL_ENTRY,
        taken_at=taken_at,
        user_confirmed_at=taken_at + timedelta(minutes=1),
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def populated_clinical_tables(
    db: Session, episode, device_profile, cuff_reading, clinician
) -> None:
    """Put at least one row in every table listed in ``CLINICAL_TABLES``.

    Needed because the append-only triggers are row-level ``BEFORE UPDATE OR DELETE`` — against
    an empty table they never fire, so a test asserting "UPDATE raises" would pass while proving
    nothing.
    """
    import uuid as _uuid

    from app.config import get_settings
    from app.models import (
        AuditAction,
        AuditLog,
        ClinicianSummary,
        DeviationState,
        MeasurementSession,
        CheckMode,
        CheckSession,
        CheckSessionStatus,
        MedicationEvent,
        MedicationStatusToday,
        PatientContext,
        Precondition,
        PregnancyAnswer,
        SessionContext,
        RedFlagEvent,
        SymptomEvent,
        TrendDirection,
        TrendEstimate,
    )
    from app.services import calibration as calibration_service

    base = datetime.now(tz=timezone.utc) - timedelta(days=20)

    def _session(
        started_at: datetime, calibration_id=None, centre_ms: float = 250.0
    ) -> MeasurementSession:
        row = MeasurementSession(
            id=_uuid.uuid4(),
            episode_id=episode.id,
            device_profile_id=device_profile.id,
            calibration_id=calibration_id,
            model_version="fixture-1.0.0",
            started_at=started_at,
            posture=Posture.SEATED,
            status=SessionStatus.COMPLETED,
            n_beats_total=50,
            n_beats_usable=50,
            ptt_ms=[centre_ms + (index % 5 - 2) * 2.0 for index in range(50)],
            quality=make_quality(),
        )
        db.add(row)
        return row

    # Distinct centres: three identical sessions would give a zero-variance baseline, which the
    # engine correctly refuses (invariant 7).
    source_sessions = [
        _session(base + timedelta(days=index), centre_ms=centre)
        for index, centre in enumerate((246.0, 250.0, 254.0))
    ]
    db.flush()

    established = calibration_service.establish(
        db,
        patient_id=episode.patient_id,
        device_profile_id=device_profile.id,
        reference_cuff_reading_id=cuff_reading.id,
        session_ids=[row.id for row in source_sessions],
        settings=get_settings(),
        now=base + timedelta(days=4),
    )

    estimated = _session(base + timedelta(days=5), established.calibration.id)
    db.flush()
    db.add(
        TrendEstimate(
            session_id=estimated.id,
            calibration_id=established.calibration.id,
            direction=TrendDirection.STABLE,
            magnitude_sd=0.4,
            confidence=0.8,
            deviation_state=DeviationState.NONE,
        )
    )

    for model in (MedicationEvent, SymptomEvent, RedFlagEvent):
        db.add(
            model(
                episode_id=episode.id,
                occurred_at=base + timedelta(days=6),
                payload={"note": "fixture"},
            )
        )

    db.add(
        PatientContext(
            patient_id=episode.patient_id,
            recorded_at=base + timedelta(days=6),
            medications=[{"name": "fixture", "dose": "1 mg"}],
            pregnant=PregnancyAnswer.NO,
            known_arrhythmia=False,
        )
    )

    check = CheckSession(
        episode_id=episode.id,
        mode=CheckMode.SENSOR,
        status=CheckSessionStatus.COMPLETED,
        started_at=base + timedelta(days=5),
    )
    db.add(check)
    db.flush()

    db.add(
        Precondition(
            check_session_id=check.id,
            recorded_at=base + timedelta(days=5),
            rested_5_min=True,
            recent_activity_30_min=False,
            recent_caffeine_30_min=False,
            recent_nicotine_30_min=False,
            needs_restroom=False,
            is_ready=True,
        )
    )

    db.add(
        SessionContext(
            check_session_id=check.id,
            recorded_at=base + timedelta(days=6),
            sleep_less_than_usual=False,
            stress_higher_than_usual=False,
            feeling_unwell=False,
            medication_status_today=MedicationStatusToday.AS_USUAL,
        )
    )

    db.add(ClinicianSummary(episode_id=episode.id, contents={"note": "fixture"}))
    db.add(
        AuditLog(
            actor="fixture",
            role=UserRole.ADMIN,
            action=AuditAction.SESSION_SUBMITTED,
            target="fixture",
        )
    )
    db.commit()


@pytest.fixture
def patient_token(client: TestClient, patient_user: AppUser) -> str:
    response = client.post(
        "/v1/auth/token",
        data={"username": patient_user.subject, "password": DEMO_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def clinician_token(client: TestClient, clinician: AppUser) -> str:
    response = client.post(
        "/v1/auth/token", data={"username": clinician.subject, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth(patient_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {patient_token}"}


@pytest.fixture
def clinician_auth(clinician_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {clinician_token}"}


# --------------------------------------------------------------------------- helpers


def make_quality(**overrides) -> dict:
    """A quality block that passes the gate, with per-test overrides."""
    quality = {
        "accel_rate_hz": 200.0,
        "camera_fps": 60.0,
        "dropped_frame_pct": 1.0,
        "snr_db": 16.0,
        "motion_index": 0.05,
        "clock_offset_ms": 0.5,
    }
    quality.update(overrides)
    return quality


def make_session_payload(
    *,
    episode: MonitoringEpisode,
    device_profile: DeviceProfile,
    started_at: datetime | None = None,
    ptt_target_ms: float = 250.0,
    n_beats: int = 50,
    status: SessionStatus = SessionStatus.COMPLETED,
    rejection_reason: str | None = None,
    ptt_ms: list[float] | None = None,
    session_id: uuid.UUID | None = None,
    **overrides,
) -> dict:
    """Build a valid session submission body.

    The intervals fan out slightly around the target so the IQR trim has something to do; a
    flat array would leave the trimming path untested.
    """
    if ptt_ms is None:
        ptt_ms = [ptt_target_ms + (index % 5 - 2) * 1.5 for index in range(n_beats)]

    payload = {
        "session_id": str(session_id or uuid.uuid4()),
        "episode_id": str(episode.id),
        "device_profile_id": str(device_profile.id),
        "model_version": "test-1.0.0",
        "started_at": (started_at or datetime.now(tz=timezone.utc)).isoformat(),
        "posture": Posture.SEATED.value,
        "status": status.value,
        "rejection_reason": rejection_reason,
        "n_beats_total": max(len(ptt_ms), n_beats),
        "n_beats_usable": len(ptt_ms),
        "ptt_ms": ptt_ms,
        "quality": make_quality(),
        "synthetic": False,
    }
    payload.update(overrides)
    return payload


def post_session(client: TestClient, auth: dict[str, str], payload: dict):
    """Submit a session the way a device does: nonce, then post with the idempotency key."""
    nonce = client.post("/v1/sessions/nonce", headers=auth).json()["nonce"]
    return client.post(
        "/v1/sessions",
        json=payload,
        headers={
            **auth,
            "X-Session-Nonce": nonce,
            "Idempotency-Key": payload["session_id"],
        },
    )
