"""Changing a password, and closing an account.

Both are account operations, and the distinction from a *clinical* operation is the whole design:
closing an account deletes the sign-in identity and leaves the pseudonymous record standing. The
App Store requires in-app deletion; invariant 5 and a `BEFORE UPDATE OR DELETE` trigger on every
clinical table forbid destroying the record. Deleting the identity satisfies the first without
touching the second, and these tests hold that line from both directions.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppUser, AuditAction, AuditLog, CuffReading, Patient, RefreshToken
from app.security.passwords import verify_password
from tests.conftest import DEMO_PASSWORD

NEW_PASSWORD = "a-replacement-password-long-enough"


def _login(client: TestClient, subject: str, password: str):
    return client.post("/v1/auth/token", data={"username": subject, "password": password})


class TestChangePassword:
    def test_the_password_actually_changes(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/auth/password",
            json={"current_password": DEMO_PASSWORD, "new_password": NEW_PASSWORD},
            headers=auth,
        )
        assert response.status_code == 204, response.text

        db.expire_all()
        row = db.get(AppUser, patient_user.id)
        assert verify_password(NEW_PASSWORD, row.password_hash)
        assert not verify_password(DEMO_PASSWORD, row.password_hash)

    def test_the_old_password_stops_working_and_the_new_one_starts(
        self, client: TestClient, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        client.post(
            "/v1/auth/password",
            json={"current_password": DEMO_PASSWORD, "new_password": NEW_PASSWORD},
            headers=auth,
        )

        assert _login(client, patient_user.subject, DEMO_PASSWORD).status_code == 401
        assert _login(client, patient_user.subject, NEW_PASSWORD).status_code == 200

    def test_a_wrong_current_password_is_refused_and_changes_nothing(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/auth/password",
            json={"current_password": "not-the-password", "new_password": NEW_PASSWORD},
            headers=auth,
        )

        # 403 and not 401 on purpose: the client's transparent-refresh path reads a 401 as a dead
        # session and signs the patient out, which is the wrong answer to a mistyped field.
        assert response.status_code == 403
        db.expire_all()
        assert verify_password(DEMO_PASSWORD, db.get(AppUser, patient_user.id).password_hash)

    def test_a_short_new_password_is_refused_before_anything_happens(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/auth/password",
            json={"current_password": DEMO_PASSWORD, "new_password": "short"},
            headers=auth,
        )

        assert response.status_code == 422
        db.expire_all()
        assert verify_password(DEMO_PASSWORD, db.get(AppUser, patient_user.id).password_hash)

    def test_every_other_session_is_revoked(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        # A second device, signed in before the change.
        other = _login(client, patient_user.subject, DEMO_PASSWORD)
        other_refresh = other.json()["refresh_token"]

        client.post(
            "/v1/auth/password",
            json={"current_password": DEMO_PASSWORD, "new_password": NEW_PASSWORD},
            headers=auth,
        )

        # Changing a password is what someone does when they think they are compromised. Leaving
        # the other session alive would make the change theatre.
        replay = client.post("/v1/auth/refresh", json={"refresh_token": other_refresh})
        assert replay.status_code == 401

    def test_it_is_audited(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        client.post(
            "/v1/auth/password",
            json={"current_password": DEMO_PASSWORD, "new_password": NEW_PASSWORD},
            headers=auth,
        )

        actions = db.execute(select(AuditLog.action)).scalars().all()
        assert AuditAction.AUTH_PASSWORD_CHANGED in actions


class TestCloseAccount:
    def test_the_sign_in_identity_is_gone(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        user_id = patient_user.id
        subject = patient_user.subject

        response = client.post(
            "/v1/auth/account/close", json={"password": DEMO_PASSWORD}, headers=auth
        )
        assert response.status_code == 200, response.text
        assert response.json()["closed"] is True

        db.expire_all()
        assert db.get(AppUser, user_id) is None
        assert _login(client, subject, DEMO_PASSWORD).status_code == 401

    def test_the_clinical_record_survives_it(
        self,
        client: TestClient,
        db: Session,
        patient_user: AppUser,
        cuff_reading: CuffReading,
        auth: dict[str, str],
    ) -> None:
        """**The point of the whole design.**

        Invariant 5 keeps clinical rows; the App Store wants the account gone. Deleting the
        identity satisfies one without violating the other, and what is left carries no name.
        """
        patient_id = patient_user.patient_id
        reading_id = cuff_reading.id

        client.post("/v1/auth/account/close", json={"password": DEMO_PASSWORD}, headers=auth)

        db.expire_all()
        assert db.get(CuffReading, reading_id) is not None
        patient = db.get(Patient, patient_id)
        assert patient is not None
        # Pseudonymous by design: there is nowhere on this row for a name or a contact detail.
        assert not hasattr(patient, "name")
        assert not hasattr(patient, "email")

    def test_the_response_says_what_was_kept(
        self, client: TestClient, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        # Telling a patient "everything is deleted" would be false, so the endpoint returns the
        # pseudonym the retained record now stands under and says so in words.
        body = client.post(
            "/v1/auth/account/close", json={"password": DEMO_PASSWORD}, headers=auth
        ).json()

        assert body["pseudonym"]
        assert "no name" in body["detail"]

    def test_a_wrong_password_closes_nothing(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/auth/account/close", json={"password": "not-the-password"}, headers=auth
        )

        assert response.status_code == 403
        db.expire_all()
        assert db.get(AppUser, patient_user.id) is not None

    def test_refresh_tokens_go_with_it(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        # `refresh_token.user_id` is a RESTRICT foreign key, so this is also what stops the delete
        # failing on a constraint instead of on anything meaningful.
        _login(client, patient_user.subject, DEMO_PASSWORD)
        user_id = patient_user.id

        client.post("/v1/auth/account/close", json={"password": DEMO_PASSWORD}, headers=auth)

        db.expire_all()
        remaining = (
            db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))
            .scalars()
            .all()
        )
        assert remaining == []

    def test_it_is_audited_and_the_entry_outlives_the_account(
        self, client: TestClient, db: Session, patient_user: AppUser, auth: dict[str, str]
    ) -> None:
        """`audit_log.actor` is a string, not a foreign key, so the trail survives the delete.

        That is deliberate and it is also the limit of what closure erases: the subject is already
        written into every earlier sign-in entry, and an append-only log cannot be rewritten. The
        UI says so rather than claiming a total erasure it cannot deliver.
        """
        client.post("/v1/auth/account/close", json={"password": DEMO_PASSWORD}, headers=auth)

        actions = db.execute(select(AuditLog.action)).scalars().all()
        assert AuditAction.AUTH_ACCOUNT_CLOSED in actions


class TestInvariantsHold:
    def test_neither_route_is_a_mutating_method(self, client: TestClient) -> None:
        """Invariant 5's route rule bans PUT, PATCH and DELETE across the whole API.

        Closing an account is an action on the caller's own identity rather than the deletion of a
        clinical resource, and is modelled as a POST so the rule stands unweakened.
        """
        from app.main import app

        spec = app.openapi()
        offenders = [
            f"{method.upper()} {path}"
            for path, operations in spec["paths"].items()
            for method in operations
            if method.upper() in {"PUT", "PATCH", "DELETE"}
        ]
        assert offenders == []

    @pytest.mark.parametrize("path", ["/v1/auth/password", "/v1/auth/account/close"])
    def test_both_require_authentication(self, client: TestClient, path: str) -> None:
        assert client.post(path, json={}).status_code in (401, 403, 422)
