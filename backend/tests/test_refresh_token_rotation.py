"""Refresh-token rotation, revocation and reuse detection.

A JWT cannot be withdrawn once signed. These tests exist because that is only acceptable if
something else can end the session, and "something else" is the ``refresh_token`` table.

The case that matters most is reuse: a token that has already been rotated is presented again.
The server cannot tell whether the attacker or the legitimate client is holding the stale copy,
so it ends the whole family. Getting that wrong in the attacker's favour costs a patient their
record; getting it wrong in the user's favour costs one login.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.models import AppUser, RefreshToken
from app.security.tokens import decode_token
from tests.conftest import DEMO_PASSWORD


def _login(client: TestClient, user: AppUser) -> dict:
    response = client.post(
        "/v1/auth/token", data={"username": user.subject, "password": DEMO_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _refresh(client: TestClient, token: str):
    return client.post("/v1/auth/refresh", json={"refresh_token": token})


@pytest.mark.invariant
def test_login_records_the_refresh_token_so_it_can_be_revoked(
    client: TestClient, patient_user: AppUser, db, settings
) -> None:
    """A refresh token that exists only as a JWT could never be taken back."""
    tokens = _login(client, patient_user)
    principal = decode_token(
        tokens["refresh_token"], expected_type="refresh", settings=settings.security
    )

    record = db.execute(
        sa.select(RefreshToken).where(RefreshToken.jti == principal.jti)
    ).scalar_one()

    assert record.user_id == patient_user.id
    assert record.is_active
    assert record.revoked_at is None
    assert record.superseded_at is None


@pytest.mark.invariant
def test_refresh_rotates_and_retires_the_old_token(
    client: TestClient, patient_user: AppUser, db, settings
) -> None:
    """Each refresh consumes its token, so a stolen one is good only until the next refresh."""
    first = _login(client, patient_user)
    second = _refresh(client, first["refresh_token"])
    assert second.status_code == 200, second.text
    second_tokens = second.json()

    assert second_tokens["refresh_token"] != first["refresh_token"]

    old_jti = decode_token(
        first["refresh_token"], expected_type="refresh", settings=settings.security
    ).jti
    new_jti = decode_token(
        second_tokens["refresh_token"], expected_type="refresh", settings=settings.security
    ).jti

    db.expire_all()
    old = db.execute(sa.select(RefreshToken).where(RefreshToken.jti == old_jti)).scalar_one()
    new = db.execute(sa.select(RefreshToken).where(RefreshToken.jti == new_jti)).scalar_one()

    assert old.superseded_at is not None, "the used token was not retired"
    assert old.replaced_by_id == new.id
    assert new.is_active
    # Same login, so the chain is traceable for revocation.
    assert new.family_id == old.family_id


@pytest.mark.invariant
def test_reusing_a_rotated_token_revokes_the_whole_family(
    client: TestClient, patient_user: AppUser, db, settings
) -> None:
    """The theft-detection case.

    Presenting an already-rotated token means two parties hold tokens from one login. Which one
    is the attacker cannot be determined from the request, so both are ended.
    """
    first = _login(client, patient_user)
    second = _refresh(client, first["refresh_token"]).json()

    # The legitimate client now holds `second`. Replay `first`, as a thief would.
    replay = _refresh(client, first["refresh_token"])
    assert replay.status_code == 401
    assert "already been used" in replay.json()["detail"]

    # The still-current token is dead too: the server could not tell who was who.
    after = _refresh(client, second["refresh_token"])
    assert after.status_code == 401, "the family was not revoked; a thief keeps access"

    family_id = decode_token(
        first["refresh_token"], expected_type="refresh", settings=settings.security
    ).jti
    db.expire_all()
    unrevoked = db.execute(
        sa.select(sa.func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == patient_user.id, RefreshToken.revoked_at.is_(None))
    ).scalar_one()
    assert unrevoked == 0, "some token in the compromised family survived"
    del family_id


@pytest.mark.invariant
def test_a_fresh_login_after_a_revocation_works(
    client: TestClient, patient_user: AppUser
) -> None:
    """Revocation ends sessions, it does not lock the account.

    The legitimate user's recovery path has to be "log in again", or reuse detection would be a
    denial-of-service on the person it protects.
    """
    first = _login(client, patient_user)
    _refresh(client, first["refresh_token"])
    _refresh(client, first["refresh_token"])  # trigger the family revocation

    recovered = _login(client, patient_user)
    assert _refresh(client, recovered["refresh_token"]).status_code == 200


@pytest.mark.invariant
def test_an_expired_refresh_token_is_refused(
    client: TestClient, patient_user: AppUser, db, settings
) -> None:
    tokens = _login(client, patient_user)
    jti = decode_token(
        tokens["refresh_token"], expected_type="refresh", settings=settings.security
    ).jti

    db.execute(
        sa.update(RefreshToken)
        .where(RefreshToken.jti == jti)
        .values(expires_at=datetime.now(tz=timezone.utc) - timedelta(seconds=1))
    )
    db.commit()

    response = _refresh(client, tokens["refresh_token"])
    assert response.status_code == 401
    assert "expired" in response.json()["detail"]


@pytest.mark.invariant
def test_a_revoked_refresh_token_is_refused(
    client: TestClient, patient_user: AppUser, db, settings
) -> None:
    tokens = _login(client, patient_user)
    jti = decode_token(
        tokens["refresh_token"], expected_type="refresh", settings=settings.security
    ).jti

    db.execute(
        sa.update(RefreshToken)
        .where(RefreshToken.jti == jti)
        .values(revoked_at=datetime.now(tz=timezone.utc), revoked_reason="test")
    )
    db.commit()

    response = _refresh(client, tokens["refresh_token"])
    assert response.status_code == 401
    assert "revoked" in response.json()["detail"]


@pytest.mark.invariant
def test_a_signed_token_with_no_record_is_refused(
    client: TestClient, patient_user: AppUser, db, settings
) -> None:
    """A valid signature is not sufficient.

    This is the case where the signing key leaked, or the database was restored from a point
    before the token was issued. Either way the session is not one to honour.
    """
    tokens = _login(client, patient_user)
    jti = decode_token(
        tokens["refresh_token"], expected_type="refresh", settings=settings.security
    ).jti

    db.execute(sa.delete(RefreshToken).where(RefreshToken.jti == jti))
    db.commit()

    response = _refresh(client, tokens["refresh_token"])
    assert response.status_code == 401
    assert "not recognised" in response.json()["detail"]


@pytest.mark.invariant
def test_an_access_token_cannot_be_used_to_refresh(
    client: TestClient, patient_user: AppUser
) -> None:
    """The ``typ`` claim is checked, not just the signature."""
    tokens = _login(client, patient_user)
    assert _refresh(client, tokens["access_token"]).status_code == 401


def test_each_login_starts_its_own_family(
    client: TestClient, patient_user: AppUser, db, settings
) -> None:
    """Revoking one compromised session must not sign the user out of their other devices."""
    phone = _login(client, patient_user)
    laptop = _login(client, patient_user)

    phone_family = _family_of(db, settings, phone["refresh_token"])
    laptop_family = _family_of(db, settings, laptop["refresh_token"])
    assert phone_family != laptop_family

    # Compromise the phone chain only.
    _refresh(client, phone["refresh_token"])
    _refresh(client, phone["refresh_token"])

    assert _refresh(client, laptop["refresh_token"]).status_code == 200, (
        "revoking one session ended an unrelated one"
    )


def _family_of(db, settings, refresh_token: str):
    jti = decode_token(
        refresh_token, expected_type="refresh", settings=settings.security
    ).jti
    return db.execute(
        sa.select(RefreshToken.family_id).where(RefreshToken.jti == jti)
    ).scalar_one()
