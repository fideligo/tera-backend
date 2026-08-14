"""Row-level scoping, and the 404-versus-403 rule that goes with it.

BUILD_SPEC 4.5: "Clinician access scoped to episodes where they are the reviewing
professional." A patient sees only their own data.

**The rule, applied consistently:**

* **404** when the caller is not entitled to know the resource exists — anything belonging to
  another patient. A 403 there confirms the id names a real row, which is a disclosure about
  someone else's care even when no field is returned. An attacker holding a list of candidate
  ids could separate the real from the invented.
* **403** when the resource is not secret but the caller lacks authority — a patient hitting an
  admin-only endpoint, or asking for the clinician summary of their *own* episode. They already
  know it exists; refusing tells them nothing new.

The tests below assert the *pair* in each case: a resource that does not exist and one that
belongs to someone else must be indistinguishable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import (
    AppUser,
    Calibration,
    CalibrationStatus,
    CameraHardwareLevel,
    CuffReading,
    CuffSource,
    DeviceProfile,
    MonitoringEpisode,
    Patient,
    QualifiedStatus,
    TimestampSource,
    UserRole,
)
from app.security.passwords import hash_password
from tests.conftest import DEMO_PASSWORD, make_session_payload, post_session


@pytest.fixture
def other_tenant(db, client: TestClient) -> dict:
    """A second patient, with their own episode, device profile and calibration.

    Everything here belongs to somebody the primary fixtures' patient has never met.
    """
    patient = Patient(
        pseudonym=f"TERA-OTHER-{uuid.uuid4().hex[:8]}",
        clinic_id="CLINIC-OTHER",
        enrolled_at=datetime.now(tz=timezone.utc) - timedelta(days=30),
    )
    db.add(patient)
    db.flush()

    clinician = AppUser(
        subject=f"other-clinician-{uuid.uuid4().hex[:8]}@test.invalid",
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.CLINICIAN,
        clinic_id="CLINIC-OTHER",
        patient_id=None,
    )
    user = AppUser(
        subject=f"other-patient-{uuid.uuid4().hex[:8]}@test.invalid",
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.PATIENT,
        clinic_id="CLINIC-OTHER",
        patient_id=patient.id,
    )
    db.add_all([clinician, user])
    db.flush()

    episode = MonitoringEpisode(
        patient_id=patient.id,
        reviewing_clinician_id=clinician.id,
        started_at=datetime.now(tz=timezone.utc) - timedelta(days=20),
        protocol_params={},
    )
    device = DeviceProfile(
        patient_id=patient.id,
        model="Other Handset",
        os_version="Android 14",
        accel_rate_hz=500.0,
        camera_fps=60.0,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.0,
        qualified_status=QualifiedStatus.QUALIFIED,
    )
    db.add_all([episode, device])
    db.flush()

    taken_at = datetime.now(tz=timezone.utc) - timedelta(days=19)
    cuff = CuffReading(
        episode_id=episode.id,
        systolic_mmhg=150,
        diastolic_mmhg=95,
        pulse_bpm=70,
        source=CuffSource.MANUAL_ENTRY,
        taken_at=taken_at,
        user_confirmed_at=taken_at,
    )
    db.add(cuff)
    db.flush()

    calibration = Calibration(
        patient_id=patient.id,
        device_profile_id=device.id,
        reference_cuff_reading_id=cuff.id,
        baseline_mean_ms=250.0,
        baseline_sd_ms=4.0,
        n_sessions=3,
        status=CalibrationStatus.ACTIVE,
        established_at=datetime.now(tz=timezone.utc) - timedelta(days=18),
    )
    db.add(calibration)
    db.commit()

    token = client.post(
        "/v1/auth/token", data={"username": user.subject, "password": DEMO_PASSWORD}
    ).json()["access_token"]
    clinician_token = client.post(
        "/v1/auth/token", data={"username": clinician.subject, "password": DEMO_PASSWORD}
    ).json()["access_token"]

    return {
        "patient": patient,
        "episode": episode,
        "device_profile": device,
        "calibration": calibration,
        "cuff_reading": cuff,
        "auth": {"Authorization": f"Bearer {token}"},
        "clinician_auth": {"Authorization": f"Bearer {clinician_token}"},
    }


# --------------------------------------------------- 404: existence must not be disclosed


@pytest.mark.invariant
def test_another_patients_episode_is_indistinguishable_from_a_missing_one(
    client: TestClient, auth, other_tenant
) -> None:
    """The whole point of the rule, stated as one assertion."""
    theirs = client.get(
        f"/v1/episodes/{other_tenant['episode'].id}/timeline", headers=auth
    )
    imaginary = client.get(f"/v1/episodes/{uuid.uuid4()}/timeline", headers=auth)

    assert theirs.status_code == 404
    assert imaginary.status_code == 404
    assert theirs.json() == imaginary.json(), (
        "a real episode and an invented one gave different answers, so the id can be probed"
    )


@pytest.mark.invariant
def test_another_patients_calibration_is_indistinguishable_from_a_missing_one(
    client: TestClient, auth, other_tenant
) -> None:
    """Regression: this returned 403 for a real row and 404 for an invented one."""
    theirs = client.get(f"/v1/calibrations/{other_tenant['calibration'].id}", headers=auth)
    imaginary = client.get(f"/v1/calibrations/{uuid.uuid4()}", headers=auth)

    assert theirs.status_code == 404
    assert imaginary.status_code == 404
    assert theirs.json() == imaginary.json()


@pytest.mark.invariant
def test_another_patients_device_profile_is_indistinguishable_from_a_missing_one(
    client: TestClient, auth, other_tenant
) -> None:
    """Regression: same leak as calibrations."""
    theirs = client.get(
        f"/v1/device-profiles/{other_tenant['device_profile'].id}", headers=auth
    )
    imaginary = client.get(f"/v1/device-profiles/{uuid.uuid4()}", headers=auth)

    assert theirs.status_code == 404
    assert imaginary.status_code == 404
    assert theirs.json() == imaginary.json()


@pytest.mark.invariant
def test_a_clinician_cannot_read_an_episode_they_do_not_review(
    client: TestClient, clinician_auth, other_tenant
) -> None:
    """Scoped to episodes where they are the reviewing professional (BUILD_SPEC 4.5)."""
    theirs = client.get(
        f"/v1/episodes/{other_tenant['episode'].id}/summary", headers=clinician_auth
    )
    imaginary = client.get(f"/v1/episodes/{uuid.uuid4()}/summary", headers=clinician_auth)

    assert theirs.status_code == 404
    assert imaginary.status_code == 404
    assert theirs.json() == imaginary.json()


@pytest.mark.invariant
def test_a_clinician_cannot_list_episodes_they_do_not_review(
    client: TestClient, clinician_auth, episode, other_tenant
) -> None:
    listed = client.get("/v1/episodes", headers=clinician_auth).json()["episodes"]
    ids = {row["episode_id"] for row in listed}

    assert str(episode.id) in ids
    assert str(other_tenant["episode"].id) not in ids


@pytest.mark.invariant
def test_a_patient_cannot_write_a_session_into_another_patients_episode(
    client: TestClient, auth, device_profile, other_tenant
) -> None:
    """Row-level scoping applies to writes, not only reads."""
    payload = make_session_payload(
        episode=other_tenant["episode"], device_profile=device_profile
    )
    assert post_session(client, auth, payload).status_code == 404


@pytest.mark.invariant
def test_a_patient_cannot_write_a_cuff_reading_into_another_patients_episode(
    client: TestClient, auth, other_tenant
) -> None:
    taken_at = datetime.now(tz=timezone.utc)
    response = client.post(
        "/v1/cuff-readings",
        headers=auth,
        json={
            "episode_id": str(other_tenant["episode"].id),
            "systolic_mmhg": 148,
            "diastolic_mmhg": 92,
            "source": "manual_entry",
            "taken_at": taken_at.isoformat(),
            "user_confirmed_at": taken_at.isoformat(),
        },
    )
    assert response.status_code == 404


@pytest.mark.invariant
def test_a_patient_cannot_record_an_event_against_another_patients_episode(
    client: TestClient, auth, other_tenant
) -> None:
    response = client.post(
        "/v1/events",
        headers=auth,
        json={
            "episode_id": str(other_tenant["episode"].id),
            "event_type": "symptom",
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            "payload": {"symptom": "test"},
        },
    )
    assert response.status_code == 404


# --------------------------------------------------- 403: authority, not secrecy


@pytest.mark.invariant
def test_a_patient_naming_another_patient_in_a_body_gets_403(
    client: TestClient, auth, other_tenant
) -> None:
    """403 is right here and leaks nothing.

    The check is a comparison against the patient_id in the caller's own token, made before any
    database lookup, so the answer is identical for a patient record that exists and one that
    does not.
    """
    real = client.post(
        "/v1/device-profiles",
        headers=auth,
        json={
            "patient_id": str(other_tenant["patient"].id),
            "model": "Probe",
            "os_version": "Android 14",
            "accel_rate_hz": 500.0,
            "camera_fps": 60.0,
            "camera_hw_level": "full",
            "manual_sensor": True,
            "timestamp_source": "realtime",
            "clock_offset_sd_ms": 1.0,
        },
    )
    invented = client.post(
        "/v1/device-profiles",
        headers=auth,
        json={
            "patient_id": str(uuid.uuid4()),
            "model": "Probe",
            "os_version": "Android 14",
            "accel_rate_hz": 500.0,
            "camera_fps": 60.0,
            "camera_hw_level": "full",
            "manual_sensor": True,
            "timestamp_source": "realtime",
            "clock_offset_sd_ms": 1.0,
        },
    )

    assert real.status_code == 403
    assert invented.status_code == 403
    assert real.json() == invented.json(), "the 403 distinguished a real patient from an invented one"


@pytest.mark.invariant
def test_a_patient_cannot_read_the_clinician_summary_of_their_own_episode(
    client: TestClient, auth, episode
) -> None:
    """403, not 404: the patient knows their own episode exists.

    Proposal, page 4: the exception summary is a "role-protected clinician web view". The data
    is theirs and is all on their timeline; the summary is written for a clinician.
    """
    response = client.get(f"/v1/episodes/{episode.id}/summary", headers=auth)

    assert response.status_code == 403
    assert "clinician view" in response.json()["detail"]

    # Their own timeline still works — this is about the view, not about withholding records.
    assert client.get(f"/v1/episodes/{episode.id}/timeline", headers=auth).status_code == 200


@pytest.mark.invariant
def test_a_patient_hitting_an_admin_only_endpoint_gets_403(client: TestClient, auth) -> None:
    """The endpoint is not secret; the caller simply lacks authority."""
    response = client.post(
        "/v1/auth/register",
        headers=auth,
        json={
            "subject": "probe@test.invalid",
            "password": "a-sufficiently-long-password",
            "role": "clinician",
        },
    )
    assert response.status_code == 403


# --------------------------------------------------- token-level bypass attempts


@pytest.mark.invariant
def test_a_forged_patient_claim_does_not_grant_access(
    client: TestClient, other_tenant, patient_user, settings
) -> None:
    """Scoping reads the token's own claims, so re-pointing them requires the signing key.

    Signed with the wrong key, the token is refused outright — the scope check is never
    reached, which is the correct order.
    """
    import jwt

    forged = jwt.encode(
        {
            "sub": patient_user.subject,
            "uid": str(patient_user.id),
            "role": "patient",
            "typ": "access",
            "pid": str(other_tenant["patient"].id),  # someone else's records
            "exp": int((datetime.now(tz=timezone.utc) + timedelta(hours=1)).timestamp()),
        },
        "not-the-signing-key",
        algorithm="HS256",
    )

    response = client.get(
        f"/v1/episodes/{other_tenant['episode'].id}/timeline",
        headers={"Authorization": f"Bearer {forged}"},
    )
    assert response.status_code == 401


@pytest.mark.invariant
def test_role_escalation_in_a_forged_token_is_refused(
    client: TestClient, patient_user
) -> None:
    """A patient minting themselves an admin claim."""
    import jwt

    forged = jwt.encode(
        {
            "sub": patient_user.subject,
            "uid": str(patient_user.id),
            "role": "admin",
            "typ": "access",
            "exp": int((datetime.now(tz=timezone.utc) + timedelta(hours=1)).timestamp()),
        },
        "not-the-signing-key",
        algorithm="HS256",
    )

    response = client.post(
        "/v1/auth/register",
        headers={"Authorization": f"Bearer {forged}"},
        json={
            "subject": "escalated@test.invalid",
            "password": "a-sufficiently-long-password",
            "role": "admin",
        },
    )
    assert response.status_code == 401


@pytest.mark.invariant
def test_an_expired_access_token_is_refused(client: TestClient, patient_user, settings) -> None:
    from app.security.tokens import Principal, issue_token

    principal = Principal(
        subject=patient_user.subject,
        role=UserRole.PATIENT,
        user_id=patient_user.id,
        patient_id=patient_user.patient_id,
    )
    expired, _ = issue_token(
        principal=principal,
        token_type="access",
        settings=settings.security,
        now=datetime.now(tz=timezone.utc) - timedelta(hours=2),
    )

    response = client.get(
        "/v1/episodes", headers={"Authorization": f"Bearer {expired}"}
    )
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


# ------------------------------------------------------------------ HIST-01 is per-patient


@pytest.mark.invariant
def test_history_never_returns_another_patients_entries(
    client: TestClient, auth, other_tenant, episode
) -> None:
    """`GET /v1/history` is scoped to the caller's own episodes, both ways.

    Written because the endpoint was reported as returning global history for every account —
    new users seeing the seeded demo record. It does not, and this is the proof rather than an
    assurance: the other tenant's 150/95 reading exists in the same database, in the same table,
    for the whole of this test, and must appear in exactly one of the two responses.

    The scoping comes from the token: `require_patient(principal)` reads `patient_id` off the
    caller's own claims and 403s rather than widening if it is absent, and `_episode_ids` turns
    that into the `WHERE episode_id IN (...)` every branch of the query uses. There is no code
    path that reaches a row outside it.
    """
    mine = client.get("/v1/history", params={"range": "all"}, headers=auth)
    assert mine.status_code == 200
    my_ids = {e["id"] for e in mine.json()["entries"]}

    theirs = client.get(
        "/v1/history", params={"range": "all"}, headers=other_tenant["auth"]
    )
    assert theirs.status_code == 200
    their_ids = {e["id"] for e in theirs.json()["entries"]}

    other_cuff_id = str(other_tenant["cuff_reading"].id)

    # The other tenant's reading belongs to them and to nobody else.
    assert other_cuff_id in their_ids
    assert other_cuff_id not in my_ids

    # And nothing at all is shared between the two records.
    assert my_ids.isdisjoint(their_ids)


@pytest.mark.invariant
def test_history_for_a_patient_with_no_records_is_empty(
    client: TestClient, db, other_tenant
) -> None:
    """A brand-new account sees `[]`, not the seeded demo episode.

    The complaint this answers was specifically that *new* accounts saw everyone's history. A
    patient with no episode has no `episode_ids` at all, which is the early return in
    `read_history` — and with data for two other patients sitting in the table.
    """
    fresh = Patient(
        pseudonym=f"TERA-FRESH-{uuid.uuid4().hex[:8]}",
        clinic_id="CLINIC-FRESH",
        enrolled_at=datetime.now(tz=timezone.utc),
    )
    db.add(fresh)
    db.flush()
    user = AppUser(
        subject=f"fresh-{uuid.uuid4().hex[:8]}@test.invalid",
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.PATIENT,
        clinic_id="CLINIC-FRESH",
        patient_id=fresh.id,
    )
    db.add(user)
    db.commit()

    token = client.post(
        "/v1/auth/token", data={"username": user.subject, "password": DEMO_PASSWORD}
    ).json()["access_token"]

    response = client.get(
        "/v1/history",
        params={"range": "all"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["entries"] == []
