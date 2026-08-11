"""The pregnancy contraindication is enforced by the server, not only by the handset.

The handset's gate is pure Dart so it survives a dead network, and it is also only a client. An
older build, a replayed request, a second client or anyone holding a token reaches the API
directly. This is the same rule enforced against whatever actually called.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import PatientContext, PregnancyAnswer, TrendEstimate
from app.services import language
from tests.conftest import make_session_payload, post_session

CONTEXT = "/v1/patient-context"


def _report(client, auth, answer: str) -> None:
    response = client.post(
        CONTEXT,
        json={"pregnant": answer, "known_arrhythmia": False},
        headers=auth,
    )
    assert response.status_code == 201


@pytest.mark.invariant
def test_a_session_is_refused_when_pregnancy_is_recorded(
    client, auth, episode, device_profile
):
    _report(client, auth, "yes")

    response = post_session(client, auth, make_session_payload(episode=episode, device_profile=device_profile))

    assert response.status_code == 403
    assert "Method unvalidated in pregnancy" in str(response.json()["detail"])


@pytest.mark.invariant
def test_the_refusal_writes_no_session_and_no_estimate(
    client, auth, db, episode, device_profile
):
    from app.models import MeasurementSession

    _report(client, auth, "yes")
    payload = make_session_payload(episode=episode, device_profile=device_profile)

    post_session(client, auth, payload)

    # Refused before anything about the capture was written. Nothing was examined, so there is no
    # rejected session to retain — the same shape as the 422 for a malformed payload.
    assert db.get(MeasurementSession, uuid.UUID(payload["session_id"])) is None
    assert db.query(TrendEstimate).count() == 0


@pytest.mark.invariant
def test_a_calibration_is_refused_too(client, auth, patient, device_profile, cuff_reading):
    _report(client, auth, "yes")

    response = client.post(
        "/v1/calibrations",
        json={
            "patient_id": str(patient.id),
            "device_profile_id": str(device_profile.id),
            "reference_cuff_reading_id": str(cuff_reading.id),
            # Three distinct ids: the schema requires that shape, and the point of this test is
            # that the gate fires before the ids are ever looked up.
            "session_ids": [str(uuid.uuid4()) for _ in range(3)],
        },
        headers=auth,
    )

    # A baseline exists only so estimates can be computed against it.
    assert response.status_code == 403
    assert "Method unvalidated in pregnancy" in str(response.json()["detail"])


@pytest.mark.invariant
def test_a_stored_estimate_is_withheld_after_pregnancy_is_reported(
    client, auth, episode, device_profile, populated_clinical_tables, db
):
    from app.models import MeasurementSession

    estimated = (
        db.query(MeasurementSession)
        .filter(MeasurementSession.estimate.has())
        .first()
    )
    assert estimated is not None

    before = client.get(f"/v1/sessions/{estimated.id}", headers=auth)
    assert before.status_code == 200
    assert before.json()["trend"] is not None

    _report(client, auth, "yes")

    after = client.get(f"/v1/sessions/{estimated.id}", headers=auth)

    assert after.status_code == 200
    # The session record stays visible; only the estimate is withheld.
    assert after.json()["trend"] is None
    assert "Method unvalidated in pregnancy" in after.json()["trend_withheld"]
    assert after.json()["session_id"] == str(estimated.id)


@pytest.mark.invariant
def test_withholding_does_not_delete_the_stored_estimate(
    client, auth, db, populated_clinical_tables
):
    before = db.query(TrendEstimate).count()
    _report(client, auth, "yes")

    assert db.query(TrendEstimate).count() == before


def test_a_session_is_accepted_when_pregnancy_is_not_reported(
    client, auth, episode, device_profile
):
    _report(client, auth, "no")

    response = post_session(client, auth, make_session_payload(episode=episode, device_profile=device_profile))

    assert response.status_code == 201


def test_prefer_not_to_say_does_not_block(
    client, auth, episode, device_profile
):
    # Blocking a declined answer would make declining functionally identical to saying yes, and
    # would coerce a disclosure the patient chose not to make. Matches the handset exactly.
    _report(client, auth, "prefer_not_to_say")

    response = post_session(client, auth, make_session_payload(episode=episode, device_profile=device_profile))

    assert response.status_code == 201


def test_a_patient_with_no_context_is_not_blocked(
    client, auth, episode, device_profile
):
    # The intake is not a precondition for using the app. Deliberate, and the weak edge of this
    # gate — see docs/decisions.md.
    response = post_session(client, auth, make_session_payload(episode=episode, device_profile=device_profile))

    assert response.status_code == 201


@pytest.mark.invariant
def test_the_latest_answer_wins_in_both_directions(
    client, auth, episode, device_profile
):
    _report(client, auth, "yes")
    assert post_session(client, auth, make_session_payload(episode=episode, device_profile=device_profile)).status_code == 403

    # patient_context is append-only, so this is a new row rather than an edit. The gate reads the
    # most recent one, so a correction takes effect immediately.
    _report(client, auth, "no")
    assert post_session(client, auth, make_session_payload(episode=episode, device_profile=device_profile)).status_code == 201


@pytest.mark.invariant
def test_the_refusal_never_says_what_the_estimate_would_have_been(client, auth):
    # Invariant 6: it names the limitation and refers on.
    words = language.CONTRAINDICATED_PREGNANCY.lower()

    assert "consult your doctor" in words
    for forbidden in ("pre-eclampsia", "preeclampsia", "dangerous", "high", "elevated", "normal"):
        assert forbidden not in words, f'"{forbidden}" is an interpretation'


@pytest.mark.invariant
def test_the_gate_reads_the_patients_own_context_not_another(
    client, auth, db, patient, episode, device_profile
):
    from app.models import Patient

    other = Patient(pseudonym=f"OTHER-{uuid.uuid4().hex[:8]}", clinic_id=None, enrolled_at=episode.started_at)
    db.add(other)
    db.flush()
    db.add(
        PatientContext(
            patient_id=other.id,
            recorded_at=episode.started_at,
            pregnant=PregnancyAnswer.YES,
            known_arrhythmia=False,
        )
    )
    db.commit()

    # Somebody else's contraindication must not block this patient.
    response = post_session(client, auth, make_session_payload(episode=episode, device_profile=device_profile))

    assert response.status_code == 201
