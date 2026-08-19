"""Invariant 4 — calibration is versioned and device-bound.

"Every estimate references the calibration in force at capture time; every calibration
references a device profile; at most one calibration is active per patient per device.
Recalibration inserts a new row and supersedes the old one — it never mutates history."
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.config import get_settings
from app.models import Calibration, CalibrationStatus, CameraHardwareLevel, DeviceProfile
from app.models import QualifiedStatus, TimestampSource, TrendEstimate
from app.services import calibration as calibration_service
from tests.conftest import make_session_payload, post_session
from tests.helpers import establish_calibration


@pytest.mark.invariant
def test_only_one_active_calibration_per_patient_per_device(
    db, patient, device_profile, cuff_reading
) -> None:
    """The partial unique index refuses a second active row for the same patient and device."""
    first = _insert_calibration(db, patient, device_profile, cuff_reading)
    assert first.status is CalibrationStatus.ACTIVE

    with pytest.raises(sa.exc.IntegrityError) as excinfo:
        _insert_calibration(db, patient, device_profile, cuff_reading)

    assert "uq_calibration_one_active_per_patient_device" in str(excinfo.value)
    db.rollback()


@pytest.mark.invariant
def test_two_devices_may_each_have_an_active_calibration(
    db, patient, device_profile, cuff_reading
) -> None:
    """The uniqueness is per *device*, because a baseline is bound to one handset's timing."""
    _insert_calibration(db, patient, device_profile, cuff_reading)

    second_device = DeviceProfile(
        patient_id=patient.id,
        model="Second Handset",
        os_version="Android 15",
        accel_rate_hz=200.0,
        camera_fps=60.0,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.0,
        qualified_status=QualifiedStatus.QUALIFIED,
    )
    db.add(second_device)
    db.flush()

    _insert_calibration(db, patient, second_device, cuff_reading)
    db.commit()

    active = db.execute(
        sa.select(Calibration).where(Calibration.status == CalibrationStatus.ACTIVE)
    ).scalars().all()
    assert len(active) == 2


@pytest.mark.invariant
def test_recalibration_supersedes_and_does_not_mutate(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """A new calibration supersedes the old one without touching its baseline."""
    first = establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    first_id = uuid.UUID(first["id"])

    original = {
        "baseline_mean_ms": first["baseline_mean_ms"],
        "baseline_sd_ms": first["baseline_sd_ms"],
        "n_sessions": first["n_sessions"],
        "device_profile_id": first["device_profile_id"],
        "reference_cuff_reading_id": first["reference_cuff_reading_id"],
        "established_at": first["established_at"],
        "source_session_ids": sorted(first["source_session_ids"]),
    }

    second = establish_calibration(
        client,
        auth,
        db,
        episode,
        device_profile,
        cuff_reading,
        ptt_targets=(236.0, 240.0, 244.0),
        effective_from=datetime.now(tz=timezone.utc) - timedelta(days=5),
    )
    second_id = uuid.UUID(second["id"])
    assert second_id != first_id, "recalibration must insert a new row"

    db.expire_all()
    old = db.get(Calibration, first_id)
    new = db.get(Calibration, second_id)

    # Supersession bookkeeping happened.
    assert old.status is CalibrationStatus.SUPERSEDED
    assert old.superseded_by_id == second_id
    assert old.superseded_at is not None
    assert new.status is CalibrationStatus.ACTIVE
    assert new.superseded_by_id is None

    # And nothing else about the old row changed.
    assert old.baseline_mean_ms == pytest.approx(original["baseline_mean_ms"])
    assert old.baseline_sd_ms == pytest.approx(original["baseline_sd_ms"])
    assert old.n_sessions == original["n_sessions"]
    assert str(old.device_profile_id) == original["device_profile_id"]
    assert str(old.reference_cuff_reading_id) == original["reference_cuff_reading_id"]
    assert sorted(str(link.session_id) for link in old.source_sessions) == original[
        "source_session_ids"
    ]

    # The new baseline is genuinely different, so this was a real recalibration.
    assert new.baseline_mean_ms != pytest.approx(old.baseline_mean_ms)


@pytest.mark.invariant
def test_calibration_baseline_cannot_be_mutated_at_database_level(
    db, patient, device_profile, cuff_reading
) -> None:
    """The trigger blocks a direct UPDATE of the baseline, not just the absence of a route."""
    calibration = _insert_calibration(db, patient, device_profile, cuff_reading)
    db.commit()

    with pytest.raises(sa.exc.DatabaseError) as excinfo:
        db.execute(
            sa.text("UPDATE calibration SET baseline_mean_ms = 999 WHERE id = :id"),
            {"id": calibration.id},
        )
    assert "immutable" in str(excinfo.value).lower()
    db.rollback()

    with pytest.raises(sa.exc.DatabaseError) as excinfo:
        db.execute(sa.text("DELETE FROM calibration WHERE id = :id"), {"id": calibration.id})
    assert "append-only" in str(excinfo.value).lower()
    db.rollback()


@pytest.mark.invariant
def test_supersession_is_one_way(db, patient, device_profile, cuff_reading) -> None:
    """A superseded calibration cannot be reactivated.

    Reactivating one would let an estimate be reinterpreted against a baseline that had
    already been retired.
    """
    first = _insert_calibration(db, patient, device_profile, cuff_reading)
    second_id = uuid.uuid4()
    first.status = CalibrationStatus.SUPERSEDED
    first.superseded_by_id = second_id
    first.superseded_at = datetime.now(tz=timezone.utc)
    db.flush()
    _insert_calibration(db, patient, device_profile, cuff_reading, calibration_id=second_id)
    db.commit()

    with pytest.raises(sa.exc.DatabaseError) as excinfo:
        db.execute(
            sa.text(
                "UPDATE calibration SET status = 'active', superseded_by_id = NULL, "
                "superseded_at = NULL WHERE id = :id"
            ),
            {"id": first.id},
        )
    assert "one-way" in str(excinfo.value).lower()
    db.rollback()


@pytest.mark.invariant
def test_estimate_references_calibration_in_force_at_capture_time(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """A session captured before a recalibration is read against the *old* baseline.

    This is the case that makes ``established_at``/``superseded_at`` necessary. The session is
    captured on day A, a recalibration happens on day B, and the session uploads on day C. If
    the server resolved "the currently active calibration" it would compare the day-A
    measurement to a reference that did not exist when it was taken.

    The two calibrations are created through the service with explicit timestamps, because the
    whole point is what happens when establishment straddles a capture — which the HTTP route,
    stamping ``established_at`` at request time, cannot express.
    """
    from app.config import get_settings

    capture_time = datetime.now(tz=timezone.utc) - timedelta(days=5)
    settings = get_settings()

    source_ids = _three_calibration_sessions(
        client, auth, episode, device_profile, base=capture_time - timedelta(days=15)
    )
    old = calibration_service.establish(
        db,
        patient_id=episode.patient_id,
        device_profile_id=device_profile.id,
        reference_cuff_reading_id=cuff_reading.id,
        session_ids=[uuid.UUID(s) for s in source_ids],
        settings=settings,
        now=capture_time - timedelta(days=1),  # in force when the session was captured
    )
    db.commit()
    old_id = old.calibration.id

    newer_ids = _three_calibration_sessions(
        client, auth, episode, device_profile,
        base=capture_time - timedelta(days=4), target=238.0,
    )
    newer = calibration_service.establish(
        db,
        patient_id=episode.patient_id,
        device_profile_id=device_profile.id,
        reference_cuff_reading_id=cuff_reading.id,
        session_ids=[uuid.UUID(s) for s in newer_ids],
        settings=settings,
        now=capture_time + timedelta(days=1),  # recalibrated *after* the capture
    )
    db.commit()
    assert newer.calibration.id != old_id
    assert newer.superseded is not None and newer.superseded.id == old_id

    # Upload the session captured before the recalibration.
    payload = make_session_payload(
        episode=episode, device_profile=device_profile, started_at=capture_time
    )
    response = post_session(client, auth, payload)
    assert response.status_code == 201, response.text

    trend = response.json()["trend"]
    assert trend is not None
    assert trend["calibration_id"] == str(old_id), (
        "the estimate was pinned to the calibration active now, not the one in force at "
        "capture time"
    )

    db.expire_all()
    stored = db.execute(
        sa.select(TrendEstimate).where(
            TrendEstimate.session_id == uuid.UUID(payload["session_id"])
        )
    ).scalar_one()
    assert stored.calibration_id == old_id


def _three_calibration_sessions(
    client, auth, episode, device_profile, *, base: datetime, target: float = 250.0
) -> list[str]:
    """Submit three accepted sessions and return their ids."""
    ids = []
    for index, offset in enumerate((-4.0, 0.0, 4.0)):
        payload = make_session_payload(
            episode=episode,
            device_profile=device_profile,
            started_at=base + timedelta(hours=index),
            ptt_target_ms=target + offset,
        )
        assert post_session(client, auth, payload).status_code == 201
        ids.append(payload["session_id"])
    return ids


@pytest.mark.invariant
def test_one_session_establishes_a_calibration(
    client: TestClient, auth, db, episode, device_profile, cuff_reading, patient
) -> None:
    """Single-point calibration, which is what the patient is actually asked to do.

    The route refused this with "a baseline needs at least 2 calibration sessions to have any
    spread at all, got 1", which blocked the whole product on hardware: the app says "take one
    cuff reading", `min_calibration_sessions` is 1, and the mmHg estimate never needed a spread —
    `pressure_estimate` fixes the intercept from one anchor and takes the slope from population
    coefficients (invariant 1). Only the deviation engine wanted an SD.
    """
    payload = make_session_payload(
        episode=episode,
        device_profile=device_profile,
        started_at=datetime.now(tz=timezone.utc) - timedelta(days=20),
        ptt_target_ms=250.0,
    )
    assert post_session(client, auth, payload).status_code == 201

    response = client.post(
        "/v1/calibrations",
        headers=auth,
        json={
            "patient_id": str(patient.id),
            "device_profile_id": str(device_profile.id),
            "reference_cuff_reading_id": str(cuff_reading.id),
            "session_ids": [payload["session_id"]],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "active"
    assert body["n_sessions"] == 1
    assert body["baseline_mean_ms"] == pytest.approx(250.0, abs=2.0)

    # The spread is the clinical floor expressed as a sigma, not an observation of this patient.
    # `n_sessions == 1` is what tells a reader which of the two it is.
    settings = get_settings().deviation
    assert body["baseline_sd_ms"] == pytest.approx(settings.trend_min_delta_ms / 2.0)


@pytest.mark.invariant
def test_no_sessions_is_still_refused(
    client: TestClient, auth, db, episode, device_profile, cuff_reading, patient
) -> None:
    """One is a policy. None is nothing to anchor to, and still a 422."""
    response = client.post(
        "/v1/calibrations",
        headers=auth,
        json={
            "patient_id": str(patient.id),
            "device_profile_id": str(device_profile.id),
            "reference_cuff_reading_id": str(cuff_reading.id),
            "session_ids": [],
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.invariant
def test_calibration_is_bound_to_one_device(
    client: TestClient, auth, db, episode, patient, device_profile, cuff_reading
) -> None:
    """Sessions from another handset cannot contribute to this device's baseline."""
    other_device = DeviceProfile(
        patient_id=patient.id,
        model="Other Handset",
        os_version="Android 13",
        accel_rate_hz=200.0,
        camera_fps=60.0,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.0,
        qualified_status=QualifiedStatus.QUALIFIED,
    )
    db.add(other_device)
    db.commit()

    session_ids = []
    for index in range(3):
        payload = make_session_payload(
            episode=episode,
            device_profile=other_device,
            started_at=datetime.now(tz=timezone.utc) - timedelta(days=20 - index),
            ptt_target_ms=250.0 + index * 2,
        )
        assert post_session(client, auth, payload).status_code == 201
        session_ids.append(payload["session_id"])

    response = client.post(
        "/v1/calibrations",
        headers=auth,
        json={
            "patient_id": str(patient.id),
            "device_profile_id": str(device_profile.id),  # a different device
            "reference_cuff_reading_id": str(cuff_reading.id),
            "session_ids": session_ids,
        },
    )
    assert response.status_code == 422, response.text
    assert "different device profile" in response.text


def test_calibration_endpoint_establishes_a_baseline(
    client: TestClient, auth, db, episode, device_profile, cuff_reading, patient
) -> None:
    """The HTTP route computes the baseline server-side from the named sessions.

    The client never supplies ``baseline_mean_ms``: a handset that could write its own baseline
    could make any later session look stable.
    """
    session_ids = _three_calibration_sessions(
        client, auth, episode, device_profile,
        base=datetime.now(tz=timezone.utc) - timedelta(days=20),
    )

    response = client.post(
        "/v1/calibrations",
        headers=auth,
        json={
            "patient_id": str(patient.id),
            "device_profile_id": str(device_profile.id),
            "reference_cuff_reading_id": str(cuff_reading.id),
            "session_ids": session_ids,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["status"] == "active"
    assert body["n_sessions"] == 3
    assert body["baseline_mean_ms"] == pytest.approx(250.0, abs=1.0)
    assert body["baseline_sd_ms"] == pytest.approx(4.0, abs=1.0)
    assert sorted(body["source_session_ids"]) == sorted(session_ids)
    assert body["superseded_by_id"] is None

    # The response body is not where the baseline comes from — the stored row agrees.
    stored = db.get(Calibration, uuid.UUID(body["id"]))
    assert stored.baseline_mean_ms == pytest.approx(body["baseline_mean_ms"])


def test_calibration_rejects_a_client_supplied_baseline(
    client: TestClient, auth, episode, device_profile, cuff_reading, patient
) -> None:
    """There is no field for it, and unknown fields are refused."""
    response = client.post(
        "/v1/calibrations",
        headers=auth,
        json={
            "patient_id": str(patient.id),
            "device_profile_id": str(device_profile.id),
            "reference_cuff_reading_id": str(cuff_reading.id),
            "session_ids": [str(uuid.uuid4()) for _ in range(3)],
            "baseline_mean_ms": 300.0,
        },
    )
    assert response.status_code == 422


@pytest.mark.invariant
def test_the_session_floor_lives_in_configuration_not_in_the_schema(
    client: TestClient, auth, episode, device_profile, cuff_reading, patient
) -> None:
    """**This test asserted the opposite, and the thing it asserted was the bug.**

    It read "fewer than three sessions is refused before it can reach the database CHECK", and that
    CHECK was `n_sessions >= 3` from BUILD_SPEC 4.3. Meanwhile `min_calibration_sessions` had been
    configuration set to 1 since the product committed to single-point calibration. The constraint
    made the configured value unreachable three layers down, so a calibration from one session was
    refused by the database whatever the setting said — and invariant 10 exists precisely to stop a
    clinical threshold living somewhere a config change cannot reach.

    What is asserted now is the property that was intended: the floor is the *setting*, and the
    schema carries only the structural minimum.
    """
    settings = get_settings().deviation
    assert settings.min_calibration_sessions == 1

    # A calibration from exactly the configured minimum is accepted end to end.
    payload = make_session_payload(episode=episode, device_profile=device_profile)
    assert post_session(client, auth, payload).status_code == 201

    response = client.post(
        "/v1/calibrations",
        headers=auth,
        json={
            "patient_id": str(patient.id),
            "device_profile_id": str(device_profile.id),
            "reference_cuff_reading_id": str(cuff_reading.id),
            "session_ids": [payload["session_id"]],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["n_sessions"] == settings.min_calibration_sessions


@pytest.mark.invariant
def test_zero_variance_baseline_is_refused(
    client: TestClient, auth, db, episode, device_profile, cuff_reading, patient
) -> None:
    """Identical calibration sessions produce no usable baseline, so none is recorded.

    Invariant 7: a zero-spread baseline would make every later session look like an extreme
    deviation. Escalating beats recording a reference that cannot mean anything.
    """
    session_ids = []
    for index in range(3):
        payload = make_session_payload(
            episode=episode,
            device_profile=device_profile,
            started_at=datetime.now(tz=timezone.utc) - timedelta(days=20 - index),
            ptt_ms=[250.0] * 50,
            n_beats=50,
        )
        assert post_session(client, auth, payload).status_code == 201
        session_ids.append(payload["session_id"])

    response = client.post(
        "/v1/calibrations",
        headers=auth,
        json={
            "patient_id": str(patient.id),
            "device_profile_id": str(device_profile.id),
            "reference_cuff_reading_id": str(cuff_reading.id),
            "session_ids": session_ids,
        },
    )
    assert response.status_code == 422, response.text
    assert "standard deviation is zero" in response.text


def _insert_calibration(
    db, patient, device_profile, cuff_reading, calibration_id=None
) -> Calibration:
    row = Calibration(
        id=calibration_id or uuid.uuid4(),
        patient_id=patient.id,
        device_profile_id=device_profile.id,
        reference_cuff_reading_id=cuff_reading.id,
        baseline_mean_ms=250.0,
        baseline_sd_ms=4.0,
        n_sessions=3,
        status=CalibrationStatus.ACTIVE,
        established_at=datetime.now(tz=timezone.utc) - timedelta(days=20),
    )
    db.add(row)
    db.flush()
    return row


def _estimate_id(db, session_id: str) -> str:
    row = db.execute(
        sa.select(TrendEstimate.id).where(TrendEstimate.session_id == uuid.UUID(session_id))
    ).scalar_one()
    return str(row)
