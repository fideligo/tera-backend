"""Session ingest contract: idempotency, nonce, rate limits and authorisation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.config import get_settings
from app.models import MeasurementSession, SessionNonce
from tests.conftest import DEMO_PASSWORD, make_session_payload, post_session
from tests.helpers import establish_calibration


@pytest.mark.invariant
def test_duplicate_session_id_returns_stored_result(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """BUILD_SPEC 4.2: "409 duplicate session_id — return the stored result unchanged"."""
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    payload = make_session_payload(episode=episode, device_profile=device_profile)

    first = post_session(client, auth, payload)
    assert first.status_code == 201, first.text

    second = post_session(client, auth, payload)
    assert second.status_code == 409, second.text
    assert second.json() == first.json(), "the stored result must come back unchanged"

    # And exactly one row exists.
    count = db.execute(
        sa.text("SELECT count(*) FROM measurement_session WHERE id = :id"),
        {"id": payload["session_id"]},
    ).scalar_one()
    assert count == 1


def test_duplicate_with_different_body_still_returns_the_stored_result(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """A retry whose body drifted does not overwrite what is on the record."""
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    payload = make_session_payload(episode=episode, device_profile=device_profile)
    first = post_session(client, auth, payload).json()

    tampered = make_session_payload(
        episode=episode,
        device_profile=device_profile,
        session_id=uuid.UUID(payload["session_id"]),
        ptt_target_ms=200.0,
    )
    second = post_session(client, auth, tampered)

    assert second.status_code == 409
    assert second.json() == first

    stored = db.get(MeasurementSession, uuid.UUID(payload["session_id"]))
    assert min(stored.ptt_ms) > 240.0, "the stored session was overwritten by the retry"


@pytest.mark.invariant
def test_nonce_cannot_be_reused(client: TestClient, auth, episode, device_profile) -> None:
    """A spent nonce returns 428."""
    nonce = client.post("/v1/sessions/nonce", headers=auth).json()["nonce"]

    first_payload = make_session_payload(episode=episode, device_profile=device_profile)
    first = client.post(
        "/v1/sessions",
        json=first_payload,
        headers={
            **auth,
            "X-Session-Nonce": nonce,
            "Idempotency-Key": first_payload["session_id"],
        },
    )
    assert first.status_code == 201, first.text

    second_payload = make_session_payload(episode=episode, device_profile=device_profile)
    second = client.post(
        "/v1/sessions",
        json=second_payload,
        headers={
            **auth,
            "X-Session-Nonce": nonce,
            "Idempotency-Key": second_payload["session_id"],
        },
    )
    assert second.status_code == 428, second.text
    assert "already been used" in second.json()["detail"]


def test_missing_nonce_returns_428(client: TestClient, auth, episode, device_profile) -> None:
    payload = make_session_payload(episode=episode, device_profile=device_profile)
    response = client.post(
        "/v1/sessions",
        json=payload,
        headers={**auth, "Idempotency-Key": payload["session_id"]},
    )
    assert response.status_code == 428
    assert "required" in response.json()["detail"]


def test_unknown_nonce_returns_428(client: TestClient, auth, episode, device_profile) -> None:
    payload = make_session_payload(episode=episode, device_profile=device_profile)
    response = client.post(
        "/v1/sessions",
        json=payload,
        headers={
            **auth,
            "X-Session-Nonce": "not-a-real-nonce",
            "Idempotency-Key": payload["session_id"],
        },
    )
    assert response.status_code == 428


def test_expired_nonce_returns_428(
    client: TestClient, auth, db, episode, device_profile
) -> None:
    nonce = client.post("/v1/sessions/nonce", headers=auth).json()["nonce"]
    db.execute(
        sa.update(SessionNonce)
        .where(SessionNonce.value == nonce)
        .values(expires_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1))
    )
    db.commit()

    payload = make_session_payload(episode=episode, device_profile=device_profile)
    response = client.post(
        "/v1/sessions",
        json=payload,
        headers={
            **auth,
            "X-Session-Nonce": nonce,
            "Idempotency-Key": payload["session_id"],
        },
    )
    assert response.status_code == 428
    assert "expired" in response.json()["detail"]


def test_nonce_is_not_transferable_between_principals(
    client: TestClient, auth, clinician_auth, episode, device_profile
) -> None:
    """A nonce issued to one subject cannot be spent by another."""
    nonce = client.post("/v1/sessions/nonce", headers=clinician_auth).json()["nonce"]

    payload = make_session_payload(episode=episode, device_profile=device_profile)
    response = client.post(
        "/v1/sessions",
        json=payload,
        headers={
            **auth,
            "X-Session-Nonce": nonce,
            "Idempotency-Key": payload["session_id"],
        },
    )
    assert response.status_code == 428
    assert "not issued to this caller" in response.json()["detail"]


def test_idempotency_key_must_equal_session_id(
    client: TestClient, auth, episode, device_profile
) -> None:
    nonce = client.post("/v1/sessions/nonce", headers=auth).json()["nonce"]
    payload = make_session_payload(episode=episode, device_profile=device_profile)

    response = client.post(
        "/v1/sessions",
        json=payload,
        headers={**auth, "X-Session-Nonce": nonce, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 400
    assert "must equal session_id" in response.json()["detail"]


def test_idempotency_check_precedes_nonce_check(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """A retry after a dropped response gets its stored result, not a 428.

    The client already spent a nonce successfully; punishing the retry for not having a fresh
    one would make a lost response unrecoverable.
    """
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    payload = make_session_payload(episode=episode, device_profile=device_profile)
    first = post_session(client, auth, payload)
    assert first.status_code == 201

    retry = client.post(
        "/v1/sessions",
        json=payload,
        headers={
            **auth,
            "X-Session-Nonce": "a-nonce-that-was-never-issued",
            "Idempotency-Key": payload["session_id"],
        },
    )
    assert retry.status_code == 409
    assert retry.json() == first.json()


def test_rate_limit_returns_429(
    client: TestClient, auth, episode, device_profile, monkeypatch
) -> None:
    """Per-token ingest limits produce 429 with Retry-After."""
    from app.security.ratelimit import limiter

    limiter.reset()
    settings = get_settings()
    monkeypatch.setattr(
        settings.security, "ingest_rate_limit_per_token_per_hour", 3, raising=True
    )
    monkeypatch.setattr(
        settings.security, "ingest_rate_limit_per_patient_per_hour", 1000, raising=True
    )

    statuses = []
    for _ in range(5):
        payload = make_session_payload(episode=episode, device_profile=device_profile)
        statuses.append(post_session(client, auth, payload).status_code)

    assert 429 in statuses, statuses
    assert statuses.count(201) == 3, statuses

    limited = post_session(
        client, auth, make_session_payload(episode=episode, device_profile=device_profile)
    )
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    limiter.reset()


def test_session_requires_authentication(client: TestClient, episode, device_profile) -> None:
    payload = make_session_payload(episode=episode, device_profile=device_profile)
    response = client.post(
        "/v1/sessions",
        json=payload,
        headers={"X-Session-Nonce": "x", "Idempotency-Key": payload["session_id"]},
    )
    assert response.status_code == 401


def test_patient_cannot_reach_another_patients_episode(
    client: TestClient, db, episode, device_profile
) -> None:
    """Cross-patient access returns 404, which does not confirm the episode exists."""
    from app.models import AppUser, Patient, UserRole
    from app.security.passwords import hash_password

    other_patient = Patient(
        pseudonym=f"TERA-OTHER-{uuid.uuid4().hex[:8]}",
        clinic_id="CLINIC-TEST",
        enrolled_at=datetime.now(tz=timezone.utc),
    )
    db.add(other_patient)
    db.flush()
    other_user = AppUser(
        subject=f"other-{uuid.uuid4().hex[:8]}@test.invalid",
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.PATIENT,
        clinic_id="CLINIC-TEST",
        patient_id=other_patient.id,
    )
    db.add(other_user)
    db.commit()

    token = client.post(
        "/v1/auth/token", data={"username": other_user.subject, "password": DEMO_PASSWORD}
    ).json()["access_token"]
    other_auth = {"Authorization": f"Bearer {token}"}

    assert (
        client.get(f"/v1/episodes/{episode.id}/timeline", headers=other_auth).status_code == 404
    )

    payload = make_session_payload(episode=episode, device_profile=device_profile)
    assert post_session(client, other_auth, payload).status_code == 404


def test_clinician_only_sees_episodes_they_review(
    client: TestClient, db, clinician_auth, episode
) -> None:
    """BUILD_SPEC 4.5 — clinician access is scoped to episodes they are reviewing."""
    from app.models import AppUser, UserRole
    from app.security.passwords import hash_password

    other_clinician = AppUser(
        subject=f"other-clinician-{uuid.uuid4().hex[:8]}@test.invalid",
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.CLINICIAN,
        clinic_id="CLINIC-TEST",
        patient_id=None,
    )
    db.add(other_clinician)
    db.commit()

    token = client.post(
        "/v1/auth/token",
        data={"username": other_clinician.subject, "password": DEMO_PASSWORD},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get(f"/v1/episodes/{episode.id}/summary", headers=headers).status_code == 404
    assert client.get("/v1/episodes", headers=headers).json()["episodes"] == []

    # The reviewing clinician does see it.
    assert (
        client.get(f"/v1/episodes/{episode.id}/summary", headers=clinician_auth).status_code
        == 200
    )


def test_refresh_token_cannot_be_used_as_an_access_token(
    client: TestClient, patient_user
) -> None:
    """A long-lived refresh token must not work where a short-lived access token belongs."""
    tokens = client.post(
        "/v1/auth/token",
        data={"username": patient_user.subject, "password": DEMO_PASSWORD},
    ).json()

    headers = {"Authorization": f"Bearer {tokens['refresh_token']}"}
    assert client.get("/v1/episodes", headers=headers).status_code == 401

    refreshed = client.post(
        "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != tokens["access_token"]


def test_unknown_user_and_wrong_password_are_indistinguishable(
    client: TestClient, patient_user
) -> None:
    """The token endpoint must not be usable to enumerate accounts."""
    unknown = client.post(
        "/v1/auth/token", data={"username": "nobody@test.invalid", "password": "x"}
    )
    wrong = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": "wrong"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


def test_session_from_unusable_device_profile_is_rejected(
    client: TestClient, auth, db, episode, device_profile
) -> None:
    """Achieved rates below the profile's qualified band are refused for a completed session."""
    payload = make_session_payload(episode=episode, device_profile=device_profile)
    payload["quality"]["accel_rate_hz"] = 80.0

    response = post_session(client, auth, payload)
    assert response.status_code == 422
    assert "qualified in" in response.text


def test_rejected_session_with_low_rates_is_still_stored(
    client: TestClient, auth, db, episode, device_profile
) -> None:
    """Invariant 3 beats the rate check.

    A session rejected for ``sensor_rate_below_qualified`` reports low rates *because* that is
    why it failed. Refusing it with 422 would discard exactly the session invariant 3 says must
    be retained.
    """
    payload = make_session_payload(
        episode=episode,
        device_profile=device_profile,
        status=__import__("app.models", fromlist=["SessionStatus"]).SessionStatus.REJECTED,
        rejection_reason="sensor_rate_below_qualified",
        ptt_ms=[],
        n_beats=15,
    )
    payload["quality"]["accel_rate_hz"] = 60.0
    payload["quality"]["camera_fps"] = 22.0

    response = post_session(client, auth, payload)
    assert response.status_code == 201, response.text
    assert db.get(MeasurementSession, uuid.UUID(payload["session_id"])) is not None
