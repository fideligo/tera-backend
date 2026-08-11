"""Self-registration for the standalone consumer app.

B2C PIVOT: there is no clinic to enrol anyone, so the account, the patient record and the first
monitoring episode are created by the person signing up.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AppUser, MonitoringEpisode, Patient, UserRole

REGISTER = "/v1/auth/register-patient"


def _credentials(subject: str = "someone@example.invalid") -> dict[str, str]:
    return {"subject": subject, "password": "a-sufficiently-long-password"}


def test_self_registration_creates_account_patient_and_episode(client, db):
    response = client.post(REGISTER, json=_credentials())

    assert response.status_code == 201
    body = response.json()

    user = db.get(AppUser, body["user"]["id"])
    patient = db.get(Patient, body["patient_id"])
    episode = db.get(MonitoringEpisode, body["episode_id"])

    assert user is not None and patient is not None and episode is not None
    assert user.role is UserRole.PATIENT
    assert user.patient_id == patient.id
    assert episode.patient_id == patient.id


def test_no_clinic_is_invented_anywhere(client, db):
    body = client.post(REGISTER, json=_credentials()).json()

    user = db.get(AppUser, body["user"]["id"])
    patient = db.get(Patient, body["patient_id"])
    episode = db.get(MonitoringEpisode, body["episode_id"])

    # A placeholder here would be a clinic affiliation that does not exist, in a clinical record.
    assert patient.clinic_id is None
    assert user.clinic_id is None
    assert episode.reviewing_clinician_id is None


def test_the_episode_is_open_and_carries_no_invented_protocol(client, db):
    body = client.post(REGISTER, json=_credentials()).json()
    episode = db.get(MonitoringEpisode, body["episode_id"])

    assert episode.ended_at is None
    # Empty: every threshold falls back to app.config rather than presenting an engineering
    # default as a clinical decision nobody made.
    assert episode.protocol_params == {}


def test_registration_is_not_flagged_synthetic(client, db):
    body = client.post(REGISTER, json=_credentials()).json()

    assert db.get(Patient, body["patient_id"]).synthetic is False
    assert db.get(AppUser, body["user"]["id"]).synthetic is False
    assert db.get(MonitoringEpisode, body["episode_id"]).synthetic is False


def test_the_returned_tokens_work_immediately(client):
    body = client.post(REGISTER, json=_credentials()).json()

    me = client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {body['tokens']['access_token']}"},
    )

    assert me.status_code == 200
    assert me.json()["patient_id"] == body["patient_id"]


def test_the_new_patient_can_reach_their_own_episode(client):
    body = client.post(REGISTER, json=_credentials()).json()
    headers = {"Authorization": f"Bearer {body['tokens']['access_token']}"}

    episodes = client.get("/v1/episodes", headers=headers)

    assert episodes.status_code == 200
    assert [e["episode_id"] for e in episodes.json()["episodes"]] == [body["episode_id"]]


def test_the_pseudonym_does_not_contain_the_subject(client):
    body = client.post(REGISTER, json=_credentials("jane.doe@example.invalid")).json()

    # BUILD_SPEC 4.1 has nowhere to put a name; deriving the pseudonym from an email address
    # would put one there sideways.
    pseudonym = body["pseudonym"].lower()
    assert "jane" not in pseudonym
    assert "doe" not in pseudonym
    assert "example" not in pseudonym


def test_a_duplicate_subject_is_refused(client):
    assert client.post(REGISTER, json=_credentials()).status_code == 201
    assert client.post(REGISTER, json=_credentials()).status_code == 409


def test_a_duplicate_subject_creates_no_orphan_patient(client, db):
    client.post(REGISTER, json=_credentials())
    before = db.execute(select(Patient)).scalars().all()

    client.post(REGISTER, json=_credentials())

    after = db.execute(select(Patient)).scalars().all()
    assert len(after) == len(before)


def test_the_endpoint_cannot_mint_a_role(client, db):
    # A role parameter on a public route is a privilege-escalation surface. The schema has no
    # such field, so supplying one is rejected outright rather than ignored.
    response = client.post(REGISTER, json={**_credentials(), "role": "admin"})

    assert response.status_code == 422


def test_a_short_password_is_refused(client):
    assert client.post(REGISTER, json={"subject": "x@y.invalid", "password": "short"}).status_code == 422


@pytest.mark.invariant
def test_self_registration_requires_no_authentication(client):
    # The route is the B2C entry point; if it ever starts demanding a token there is no way in.
    assert client.post(REGISTER, json=_credentials()).status_code == 201
