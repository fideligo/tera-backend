"""The audit trail for authentication and clinician access.

The proposal lists "audit trails" among its design controls (Table B1). In practice that has to
mean every authentication event and every clinician access to a patient record is attributable
afterwards — including the events that failed, which are the ones an attack looks like.

Every test here also checks that no clinical content reached the log. An audit entry says *that*
something happened and to which row, never what the value was.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.models import AuditAction, AuditLog
from tests.conftest import DEMO_PASSWORD


def _entries(db, action: AuditAction) -> list[AuditLog]:
    return list(
        db.execute(sa.select(AuditLog).where(AuditLog.action == action)).scalars().all()
    )


@pytest.mark.invariant
def test_a_failed_login_is_recorded_with_the_attempted_subject(
    client: TestClient, db, patient_user
) -> None:
    """Wrong password against a real account."""
    response = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": "wrong"}
    )
    assert response.status_code == 401

    db.expire_all()
    entries = _entries(db, AuditAction.AUTH_LOGIN_FAILED)
    assert len(entries) == 1
    assert entries[0].actor == patient_user.subject
    # Not authenticated, so there is no role to record and none is invented.
    assert entries[0].role is None


@pytest.mark.invariant
def test_a_failed_login_against_an_unknown_account_is_also_recorded(
    client: TestClient, db
) -> None:
    """The credential-stuffing case.

    Dropping these would blind the audit trail to exactly the attack it most needs to show:
    someone spraying a password list across accounts that may not exist.
    """
    response = client.post(
        "/v1/auth/token", data={"username": "nobody@test.invalid", "password": "guess"}
    )
    assert response.status_code == 401

    db.expire_all()
    entries = _entries(db, AuditAction.AUTH_LOGIN_FAILED)
    assert len(entries) == 1
    assert entries[0].actor == "nobody@test.invalid"
    assert entries[0].role is None


def test_repeated_failures_against_one_account_are_countable(
    client: TestClient, db, patient_user
) -> None:
    """The point of recording the subject: a burst against one account has to be visible."""
    for _ in range(4):
        client.post(
            "/v1/auth/token", data={"username": patient_user.subject, "password": "wrong"}
        )

    db.expire_all()
    entries = _entries(db, AuditAction.AUTH_LOGIN_FAILED)
    assert len([e for e in entries if e.actor == patient_user.subject]) == 4


def test_an_attacker_supplied_subject_cannot_overflow_the_column(
    client: TestClient, db
) -> None:
    """The actor is attacker-controlled, so it is bounded rather than trusted."""
    response = client.post(
        "/v1/auth/token", data={"username": "x" * 5000, "password": "guess"}
    )
    assert response.status_code in (401, 422)

    db.expire_all()
    for entry in _entries(db, AuditAction.AUTH_LOGIN_FAILED):
        assert len(entry.actor) <= 128


@pytest.mark.invariant
def test_a_successful_login_is_recorded(client: TestClient, db, patient_user) -> None:
    assert (
        client.post(
            "/v1/auth/token",
            data={"username": patient_user.subject, "password": DEMO_PASSWORD},
        ).status_code
        == 200
    )

    db.expire_all()
    entries = _entries(db, AuditAction.AUTH_TOKEN_ISSUED)
    assert len(entries) == 1
    assert entries[0].actor == patient_user.subject
    assert entries[0].role is not None, "an authenticated actor has a role"


@pytest.mark.invariant
def test_a_refresh_is_recorded(client: TestClient, db, patient_user) -> None:
    tokens = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()
    assert (
        client.post(
            "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 200
    )

    db.expire_all()
    assert len(_entries(db, AuditAction.AUTH_TOKEN_REFRESHED)) == 1


@pytest.mark.invariant
def test_refresh_token_reuse_is_recorded_as_its_own_action(
    client: TestClient, db, patient_user
) -> None:
    """Distinct from an ordinary expiry, because it is a security incident.

    Someone replayed a token that had already been rotated. Recording it under the same action
    as a routine failure would bury it.
    """
    tokens = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()
    client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    replay = client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert replay.status_code == 401

    db.expire_all()
    entries = _entries(db, AuditAction.AUTH_REFRESH_REUSE_DETECTED)
    assert len(entries) == 1, "the incident was not recorded"
    assert entries[0].actor == patient_user.subject


@pytest.mark.invariant
def test_the_reuse_record_survives_the_failed_request(
    client: TestClient, db, patient_user
) -> None:
    """The request raises 401; the audit entry must not be rolled back with it."""
    tokens = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()
    client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    client.post("/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    db.expire_all()
    assert _entries(db, AuditAction.AUTH_REFRESH_REUSE_DETECTED), (
        "the incident record was rolled back with the failed request"
    )


@pytest.mark.invariant
def test_logout_is_recorded(client: TestClient, db, patient_user) -> None:
    tokens = client.post(
        "/v1/auth/token", data={"username": patient_user.subject, "password": DEMO_PASSWORD}
    ).json()
    client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"refresh_token": tokens["refresh_token"]},
    )

    db.expire_all()
    assert len(_entries(db, AuditAction.AUTH_LOGOUT)) == 1


@pytest.mark.invariant
def test_clinician_access_to_an_episode_is_recorded(
    client: TestClient, db, clinician_auth, clinician, episode
) -> None:
    """Every clinician read of a patient record is attributable."""
    assert (
        client.get(f"/v1/episodes/{episode.id}/summary", headers=clinician_auth).status_code
        == 200
    )
    assert (
        client.get(f"/v1/episodes/{episode.id}/timeline", headers=clinician_auth).status_code
        == 200
    )

    db.expire_all()
    summary_entries = _entries(db, AuditAction.SUMMARY_GENERATED)
    timeline_entries = _entries(db, AuditAction.TIMELINE_VIEWED)

    assert [e.target for e in summary_entries] == [str(episode.id)]
    assert [e.target for e in timeline_entries] == [str(episode.id)]
    assert summary_entries[0].actor == clinician.subject


@pytest.mark.invariant
def test_a_denied_clinician_view_is_recorded(client: TestClient, db, auth, episode) -> None:
    """A patient reaching for the clinician summary is refused, and the attempt is kept."""
    assert client.get(f"/v1/episodes/{episode.id}/summary", headers=auth).status_code == 403

    db.expire_all()
    entries = _entries(db, AuditAction.CLINICIAN_ACCESS_DENIED)
    assert len(entries) == 1
    assert entries[0].target == str(episode.id)


@pytest.mark.invariant
def test_no_audit_entry_carries_clinical_content(
    client: TestClient, db, auth, clinician_auth, episode, patient_user
) -> None:
    """An entry says what happened and to which row, never what the value was."""
    from datetime import datetime, timezone

    taken_at = datetime.now(tz=timezone.utc)
    assert (
        client.post(
            "/v1/cuff-readings",
            headers=auth,
            json={
                "episode_id": str(episode.id),
                "systolic_mmhg": 191,
                "diastolic_mmhg": 117,
                "pulse_bpm": 143,
                "source": "manual_entry",
                "taken_at": taken_at.isoformat(),
                "user_confirmed_at": taken_at.isoformat(),
            },
        ).status_code
        == 201
    )
    client.get(f"/v1/episodes/{episode.id}/summary", headers=clinician_auth)

    db.expire_all()
    rows = db.execute(sa.select(AuditLog)).scalars().all()
    assert rows, "nothing was audited, so this test proved nothing"

    for row in rows:
        blob = f"{row.actor}|{row.target}|{row.action.value}"
        for marker in ("191", "117", "143"):
            assert marker not in blob, f"audit entry leaked a clinical value: {blob}"


@pytest.mark.invariant
def test_the_audit_log_remains_append_only(db, patient_user) -> None:
    """Adding a nullable role must not have loosened the trigger."""
    db.add(
        AuditLog(actor="probe", role=None, action=AuditAction.AUTH_LOGIN_FAILED, target=None)
    )
    db.commit()

    with pytest.raises(sa.exc.DatabaseError) as excinfo:
        db.execute(sa.text("UPDATE audit_log SET actor = 'rewritten'"))
    assert "append-only" in str(excinfo.value).lower()
    db.rollback()

    with pytest.raises(sa.exc.DatabaseError):
        db.execute(sa.text("DELETE FROM audit_log"))
    db.rollback()


def test_an_unauthenticated_entry_records_no_role(db) -> None:
    """The schema permits it, which is what makes the honest answer recordable."""
    entry = AuditLog(
        actor="someone@test.invalid",
        role=None,
        action=AuditAction.AUTH_LOGIN_FAILED,
        target=None,
    )
    db.add(entry)
    db.commit()

    stored = db.execute(
        sa.select(AuditLog).where(AuditLog.id == entry.id)
    ).scalar_one()
    assert stored.role is None
