"""The auth surface: register, login, refresh, logout, me.

Registration is admin-only because the proposal describes enrolment as clinic-initiated — a
patient is enrolled into a monitoring episode by a clinic when treatment is adjusted. A public
sign-up form would let anyone create an account holding clinical data with no clinic behind it.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.models import AppUser, Patient, UserRole
from app.security.passwords import hash_password
from tests.conftest import DEMO_PASSWORD

STRONG_PASSWORD = "a-sufficiently-long-password"


@pytest.fixture
def admin_user(db) -> AppUser:
    row = AppUser(
        subject=f"admin-{uuid.uuid4().hex[:8]}@test.invalid",
        password_hash=hash_password(DEMO_PASSWORD),
        role=UserRole.ADMIN,
        clinic_id="CLINIC-TEST",
        patient_id=None,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def admin_auth(client: TestClient, admin_user: AppUser) -> dict[str, str]:
    token = client.post(
        "/v1/auth/token", data={"username": admin_user.subject, "password": DEMO_PASSWORD}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- register


@pytest.mark.invariant
def test_register_requires_admin(client: TestClient, auth, clinician_auth) -> None:
    """A patient or clinician cannot mint accounts."""
    body = {
        "subject": "someone@test.invalid",
        "password": STRONG_PASSWORD,
        "role": "clinician",
    }

    assert client.post("/v1/auth/register", headers=auth, json=body).status_code == 403
    assert client.post("/v1/auth/register", headers=clinician_auth, json=body).status_code == 403
    # And anonymously.
    assert client.post("/v1/auth/register", json=body).status_code == 401


def test_admin_can_register_a_clinician(client: TestClient, admin_auth) -> None:
    subject = f"new-clinician-{uuid.uuid4().hex[:8]}@test.invalid"
    response = client.post(
        "/v1/auth/register",
        headers=admin_auth,
        json={
            "subject": subject,
            "password": STRONG_PASSWORD,
            "role": "clinician",
            "clinic_id": "CLINIC-TEST",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["subject"] == subject
    assert body["role"] == "clinician"
    assert body["patient_id"] is None
    assert body["active_sessions"] == 0
    # The response is identity only; no password material of any kind comes back.
    assert "password" not in response.text
    assert "hash" not in response.text

    # The new account works.
    assert (
        client.post(
            "/v1/auth/token", data={"username": subject, "password": STRONG_PASSWORD}
        ).status_code
        == 200
    )


def test_registering_a_patient_account_requires_a_patient_record(
    client: TestClient, admin_auth, patient: Patient
) -> None:
    """The schema and the database CHECK agree: a patient login points at a patient row."""
    without = client.post(
        "/v1/auth/register",
        headers=admin_auth,
        json={
            "subject": f"p-{uuid.uuid4().hex[:8]}@test.invalid",
            "password": STRONG_PASSWORD,
            "role": "patient",
        },
    )
    assert without.status_code == 422

    with_record = client.post(
        "/v1/auth/register",
        headers=admin_auth,
        json={
            "subject": f"p-{uuid.uuid4().hex[:8]}@test.invalid",
            "password": STRONG_PASSWORD,
            "role": "patient",
            "patient_id": str(patient.id),
        },
    )
    assert with_record.status_code == 201, with_record.text


def test_a_non_patient_account_may_not_claim_a_patient_record(
    client: TestClient, admin_auth, patient: Patient
) -> None:
    response = client.post(
        "/v1/auth/register",
        headers=admin_auth,
        json={
            "subject": f"c-{uuid.uuid4().hex[:8]}@test.invalid",
            "password": STRONG_PASSWORD,
            "role": "clinician",
            "patient_id": str(patient.id),
        },
    )
    assert response.status_code == 422


def test_registering_an_unknown_patient_record_is_refused(
    client: TestClient, admin_auth
) -> None:
    response = client.post(
        "/v1/auth/register",
        headers=admin_auth,
        json={
            "subject": f"p-{uuid.uuid4().hex[:8]}@test.invalid",
            "password": STRONG_PASSWORD,
            "role": "patient",
            "patient_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 422
    assert "does not name an existing patient" in response.text


def test_duplicate_subject_is_refused(client: TestClient, admin_auth, patient_user) -> None:
    response = client.post(
        "/v1/auth/register",
        headers=admin_auth,
        json={
            "subject": patient_user.subject,
            "password": STRONG_PASSWORD,
            "role": "clinician",
        },
    )
    assert response.status_code == 409


def test_a_short_password_is_refused(client: TestClient, admin_auth) -> None:
    """bcrypt silently ignores input past 72 bytes, so length is enforced at both ends."""
    response = client.post(
        "/v1/auth/register",
        headers=admin_auth,
        json={"subject": "short@test.invalid", "password": "short", "role": "clinician"},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- me


def test_me_returns_identity_and_no_clinical_content(
    client: TestClient, auth, patient_user: AppUser, patient: Patient
) -> None:
    response = client.get("/v1/auth/me", headers=auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["subject"] == patient_user.subject
    assert body["role"] == "patient"
    assert body["patient_id"] == str(patient.id)
    assert body["active_sessions"] >= 1

    # An identity endpoint is not a data endpoint.
    for forbidden in ("systolic", "diastolic", "mmhg", "ptt", "password", "hash"):
        assert forbidden not in response.text.lower()


def test_me_requires_a_token(client: TestClient) -> None:
    assert client.get("/v1/auth/me").status_code == 401


# --------------------------------------------------------------------------- logout


@pytest.mark.invariant
def test_logout_revokes_the_refresh_token(client: TestClient, patient_user: AppUser) -> None:
    tokens = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert (
        client.post(
            "/v1/auth/logout", headers=headers, json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 204
    )

    after = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert after.status_code == 401, "the session survived logout"


def test_logout_is_idempotent(client: TestClient, patient_user: AppUser) -> None:
    """A client clearing local state should not have to handle an error to do it."""
    tokens = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    body = {"refresh_token": tokens["refresh_token"]}

    assert client.post("/v1/auth/logout", headers=headers, json=body).status_code == 204
    assert client.post("/v1/auth/logout", headers=headers, json=body).status_code == 204


@pytest.mark.invariant
def test_logout_all_ends_every_device(client: TestClient, patient_user: AppUser) -> None:
    phone = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()
    laptop = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()

    response = client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {phone['access_token']}"},
        json={"all_sessions": True},
    )
    assert response.status_code == 204

    for name, tokens in (("phone", phone), ("laptop", laptop)):
        assert (
            client.post(
                "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            ).status_code
            == 401
        ), f"{name} session survived logout-all"


@pytest.mark.invariant
def test_logout_cannot_end_another_accounts_session(
    client: TestClient, auth, clinician: AppUser
) -> None:
    """Holding someone else's refresh token must not be a way to sign them out."""
    victim = client.post(
        "/v1/auth/token", data={"username": clinician.subject, "password": DEMO_PASSWORD}
    ).json()

    response = client.post(
        "/v1/auth/logout", headers=auth, json={"refresh_token": victim["refresh_token"]}
    )
    assert response.status_code == 403

    # The victim's session still works.
    assert (
        client.post(
            "/v1/auth/refresh", json={"refresh_token": victim["refresh_token"]}
        ).status_code
        == 200
    )


def test_logout_needs_something_to_revoke(client: TestClient, auth) -> None:
    assert client.post("/v1/auth/logout", headers=auth, json={}).status_code == 422


def test_logout_requires_authentication(client: TestClient, patient_user: AppUser) -> None:
    tokens = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()
    assert (
        client.post(
            "/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
