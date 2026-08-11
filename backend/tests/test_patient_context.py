"""Patient-supplied clinical context (B2C pivot)."""

from __future__ import annotations

import pytest

CONTEXT = "/v1/patient-context"


def _body(**overrides) -> dict:
    body = {
        "last_regimen_change_date": "2026-06-15T00:00:00Z",
        "medications": [{"name": "Amlodipine", "dose": "5 mg once daily"}],
        "pregnant": "no",
        "known_arrhythmia": False,
        "last_clinic_systolic_mmhg": 148,
        "last_clinic_diastolic_mmhg": 92,
        "last_clinic_taken_on": "2026-07-01T00:00:00Z",
    }
    body.update(overrides)
    return body


def test_all_five_fields_round_trip(client, auth):
    created = client.post(CONTEXT, json=_body(), headers=auth)
    assert created.status_code == 201

    body = client.get(CONTEXT, headers=auth).json()

    assert body["last_regimen_change_date"].startswith("2026-06-15")
    assert body["medications"] == [{"name": "Amlodipine", "dose": "5 mg once daily"}]
    assert body["pregnant"] == "no"
    assert body["known_arrhythmia"] is False
    assert body["last_clinic_systolic_mmhg"] == 148
    assert body["last_clinic_diastolic_mmhg"] == 92
    assert body["last_clinic_taken_on"].startswith("2026-07-01")


def test_pregnancy_is_three_valued(client, auth):
    for answer in ("yes", "no", "prefer_not_to_say"):
        assert client.post(CONTEXT, json=_body(pregnant=answer), headers=auth).status_code == 201
        assert client.get(CONTEXT, headers=auth).json()["pregnant"] == answer


def test_the_optional_fields_may_all_be_absent(client, auth):
    response = client.post(
        CONTEXT,
        json={"pregnant": "prefer_not_to_say", "known_arrhythmia": True},
        headers=auth,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["medications"] == []
    assert body["last_regimen_change_date"] is None
    assert body["last_clinic_systolic_mmhg"] is None


def test_a_later_submission_supersedes_without_erasing(client, auth, db):
    from app.models import PatientContext

    client.post(CONTEXT, json=_body(pregnant="no"), headers=auth)
    client.post(CONTEXT, json=_body(pregnant="yes"), headers=auth)

    # Latest wins on read...
    assert client.get(CONTEXT, headers=auth).json()["pregnant"] == "yes"
    # ...and the earlier answer is still on the record. What the patient said in June is a fact
    # about June.
    assert len(db.query(PatientContext).all()) == 2


@pytest.mark.invariant
def test_context_rows_cannot_be_updated_or_deleted(client, auth, db):
    import sqlalchemy as sa

    created = client.post(CONTEXT, json=_body(), headers=auth).json()

    with pytest.raises(Exception):
        db.execute(
            sa.text("UPDATE patient_context SET known_arrhythmia = true WHERE id = :id"),
            {"id": created["id"]},
        )
    db.rollback()

    with pytest.raises(Exception):
        db.execute(
            sa.text("DELETE FROM patient_context WHERE id = :id"), {"id": created["id"]}
        )
    db.rollback()


def test_a_clinic_reading_needs_both_numbers_and_a_date(client, auth):
    partial = client.post(
        CONTEXT, json=_body(last_clinic_taken_on=None), headers=auth
    )
    assert partial.status_code == 422

    no_numbers = client.post(
        CONTEXT,
        json=_body(last_clinic_systolic_mmhg=None, last_clinic_diastolic_mmhg=None),
        headers=auth,
    )
    assert no_numbers.status_code == 422


def test_a_swapped_clinic_reading_is_refused(client, auth):
    response = client.post(
        CONTEXT,
        json=_body(last_clinic_systolic_mmhg=80, last_clinic_diastolic_mmhg=120),
        headers=auth,
    )

    assert response.status_code == 422


def test_the_medication_list_is_bounded(client, auth):
    response = client.post(
        CONTEXT,
        json=_body(medications=[{"name": f"drug-{i}", "dose": "1"} for i in range(33)]),
        headers=auth,
    )

    # Invariant 2 in spirit: an unbounded JSONB column on an ingest route is where a series ends up.
    assert response.status_code == 422


def test_context_is_filed_against_the_token_not_the_body(client, auth):
    # There is no patient_id field, so naming someone else's record is not expressible.
    response = client.post(
        CONTEXT,
        json={**_body(), "patient_id": "00000000-0000-0000-0000-000000000000"},
        headers=auth,
    )

    assert response.status_code == 422


def test_a_clinician_has_no_clinical_context(client, clinician_auth):
    assert client.post(CONTEXT, json=_body(), headers=clinician_auth).status_code == 403
    assert client.get(CONTEXT, headers=clinician_auth).status_code == 403


def test_reading_before_anything_was_recorded_is_a_404(client, auth):
    assert client.get(CONTEXT, headers=auth).status_code == 404


def test_the_route_requires_authentication(client):
    assert client.post(CONTEXT, json=_body()).status_code == 401
    assert client.get(CONTEXT).status_code == 401


@pytest.mark.invariant
def test_a_recorded_context_is_not_flagged_synthetic(client, auth):
    assert client.post(CONTEXT, json=_body(), headers=auth).json()["synthetic"] is False
