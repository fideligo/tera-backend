"""PHR profile, session context and the insight endpoint (PM spec sections 28 and 30)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.models import PhrProfile, SessionContext
from tests.conftest import make_session_payload, post_session

PROFILE = "/v1/profile"


def _context_url(session_id) -> str:
    return f"/v1/check-sessions/{session_id}/context"


def _insight_url(session_id) -> str:
    return f"/v1/check-sessions/{session_id}/insight"


class TestProfile:
    def test_patch_creates_then_updates_in_place(self, client, auth, db):
        first = client.post(
            PROFILE,
            json={"date_of_birth": "1974-03-02", "sex_assigned_at_birth": "female"},
            headers=auth,
        )
        assert first.status_code == 200

        second = client.post(PROFILE, json={"height_cm": 162}, headers=auth)
        assert second.status_code == 200

        # A profile describes a person now, so this is one mutable row, not a history.
        assert db.query(PhrProfile).count() == 1
        body = second.json()
        assert body["date_of_birth"] == "1974-03-02"
        assert body["height_cm"] == 162

    def test_an_absent_field_means_unchanged_not_cleared(self, client, auth):
        client.post(PROFILE, json={"weight_kg": 71.5}, headers=auth)
        after = client.post(PROFILE, json={"taking_bp_medication": True}, headers=auth).json()

        # Otherwise a screen collecting half the profile would erase the other half on save.
        assert after["weight_kg"] == 71.5
        assert after["taking_bp_medication"] is True

    def test_all_the_onb_fields_round_trip(self, client, auth):
        response = client.post(
            PROFILE,
            json={
                "date_of_birth": "1980-01-15",
                "sex_assigned_at_birth": "male",
                "height_cm": 175,
                "weight_kg": 82.4,
                "hypertension_status": "diagnosed",
                "taking_bp_medication": True,
                "conditions": ["diabetes", "high_cholesterol"],
            },
            headers=auth,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["hypertension_status"] == "diagnosed"
        assert sorted(body["conditions"]) == ["diabetes", "high_cholesterol"]

    def test_a_future_date_of_birth_is_refused(self, client, auth):
        assert (
            client.post(PROFILE, json={"date_of_birth": "2999-01-01"}, headers=auth).status_code
            == 422
        )

    def test_implausible_height_and_weight_are_refused(self, client, auth):
        assert client.post(PROFILE, json={"height_cm": 1620}, headers=auth).status_code == 422
        assert client.post(PROFILE, json={"weight_kg": 715}, headers=auth).status_code == 422

    def test_an_unknown_condition_code_is_refused(self, client, auth):
        # A closed list, so a typo is a 422 rather than a row nobody can query later.
        response = client.post(PROFILE, json={"conditions": ["definitely_not_real"]}, headers=auth)

        assert response.status_code == 422

    def test_no_bmi_is_returned(self, client, auth):
        body = client.post(
            PROFILE, json={"height_cm": 162, "weight_kg": 71.5}, headers=auth
        ).json()

        # The spec forbids deriving one and invariant 6 forbids the class of thing.
        assert not [k for k in body if "bmi" in k.lower()]

    def test_the_profile_is_scoped_to_the_token(self, client, auth, clinician_auth):
        # There is no patient_id in the schema, so editing someone else's is not expressible.
        assert client.post(PROFILE, json={"height_cm": 170}, headers=clinician_auth).status_code == 403
        assert client.get(PROFILE, headers=clinician_auth).status_code == 403

    def test_reading_before_anything_was_saved_is_a_404(self, client, auth):
        assert client.get(PROFILE, headers=auth).status_code == 404

    def test_the_route_requires_authentication(self, client):
        assert client.post(PROFILE, json={"height_cm": 170}).status_code == 401


class TestSessionContext:
    @pytest.fixture
    def session_id(self, client, auth, episode, device_profile):
        payload = make_session_payload(episode=episode, device_profile=device_profile)
        response = post_session(client, auth, payload)
        assert response.status_code == 201
        return response.json()["session_id"]

    def test_context_is_recorded_against_the_session(self, client, auth, session_id):
        response = client.post(
            _context_url(session_id),
            json={
                "sleep_less_than_usual": True,
                "stress_higher_than_usual": False,
                "feeling_unwell": True,
                "symptoms": ["headache", "dizziness"],
                "medication_status_today": "missed_or_late",
            },
            headers=auth,
        )

        assert response.status_code == 201
        body = response.json()
        assert body["session_id"] == session_id
        assert body["sleep_less_than_usual"] is True
        assert sorted(body["symptoms"]) == ["dizziness", "headache"]
        assert body["medication_status_today"] == "missed_or_late"

    def test_a_correction_appends_and_the_latest_wins(self, client, auth, db, session_id):
        client.post(
            _context_url(session_id),
            json={"feeling_unwell": True, "medication_status_today": "as_usual"},
            headers=auth,
        )
        client.post(
            _context_url(session_id),
            json={"feeling_unwell": False, "medication_status_today": "as_usual"},
            headers=auth,
        )

        # What the patient reported around a past measurement is a fact about that moment.
        assert db.query(SessionContext).count() == 2
        assert client.get(_context_url(session_id), headers=auth).json()["feeling_unwell"] is False

    @pytest.mark.invariant
    def test_context_rows_cannot_be_updated_or_deleted(self, client, auth, db, session_id):
        created = client.post(
            _context_url(session_id), json={"feeling_unwell": True}, headers=auth
        ).json()

        with pytest.raises(Exception):
            db.execute(
                sa.text("UPDATE session_context SET feeling_unwell = false WHERE id = :id"),
                {"id": created["id"]},
            )
        db.rollback()

        with pytest.raises(Exception):
            db.execute(
                sa.text("DELETE FROM session_context WHERE id = :id"), {"id": created["id"]}
            )
        db.rollback()

    def test_an_unknown_symptom_code_is_refused(self, client, auth, session_id):
        response = client.post(
            _context_url(session_id), json={"symptoms": ["chest_pain"]}, headers=auth
        )

        # chest_pain is a red flag: it terminates a session before capture, offline. Arriving here
        # it would be arriving too late to act on, so the closed list refuses it.
        assert response.status_code == 422

    def test_defaults_describe_an_unremarkable_day(self, client, auth, session_id):
        body = client.post(_context_url(session_id), json={}, headers=auth).json()

        assert body["sleep_less_than_usual"] is False
        assert body["symptoms"] == []
        assert body["medication_status_today"] == "not_sure"

    def test_context_for_an_unknown_session_is_a_404(self, client, auth):
        response = client.post(
            _context_url("00000000-0000-0000-0000-000000000000"), json={}, headers=auth
        )

        assert response.status_code == 404

    def test_reading_before_anything_was_recorded_is_a_404(self, client, auth, session_id):
        assert client.get(_context_url(session_id), headers=auth).status_code == 404


class TestInsight:
    @pytest.fixture
    def session_id(self, client, auth, episode, device_profile):
        payload = make_session_payload(episode=episode, device_profile=device_profile)
        return post_session(client, auth, payload).json()["session_id"]

    def test_an_insight_is_returned_with_wording_for_every_code(self, client, auth, session_id):
        response = client.get(_insight_url(session_id), headers=auth)

        assert response.status_code == 200
        body = response.json()
        assert body["result_state"]
        assert body["priority_action_code"]
        # A code with no sentence would render as a blank space on a patient's screen.
        assert body["hero"]
        assert body["next_best_step"]

    def test_it_carries_the_disclaimer_and_the_scope_notice(
        self, client, auth, session_id
    ):
        body = client.get(_insight_url(session_id), headers=auth).json()

        assert "does not assume they caused" in body["context_disclaimer"]
        assert "does not identify or rule out" in body["notice"].lower()

    def test_context_is_reflected_in_the_insight(self, client, auth, session_id):
        client.post(
            _context_url(session_id),
            json={"sleep_less_than_usual": True, "medication_status_today": "missed_or_late"},
            headers=auth,
        )

        body = client.get(_insight_url(session_id), headers=auth).json()

        assert "less_sleep" in body["context_codes"]
        assert "medication_missed" in body["context_codes"]
        assert body["around_this_check"]["sleep_less_than_usual"] is True

    def test_reading_it_twice_gives_the_same_verdict(self, client, auth, session_id):
        first = client.get(_insight_url(session_id), headers=auth).json()
        second = client.get(_insight_url(session_id), headers=auth).json()

        # Computed on read and stored nowhere, so there is no second copy to drift.
        assert first["result_state"] == second["result_state"]
        assert first["priority_action_code"] == second["priority_action_code"]

    @pytest.mark.invariant
    def test_the_contraindication_gate_covers_the_insight_too(self, client, auth, session_id):
        # An estimate withheld on the session detail must not reappear wrapped in an insight.
        client.post(
            "/v1/patient-context",
            json={"pregnant": "yes", "known_arrhythmia": False},
            headers=auth,
        )

        response = client.get(_insight_url(session_id), headers=auth)

        assert response.status_code == 403
        assert "Method unvalidated in pregnancy" in str(response.json()["detail"])

    def test_an_unknown_session_is_a_404(self, client, auth):
        assert (
            client.get(
                _insight_url("00000000-0000-0000-0000-000000000000"), headers=auth
            ).status_code
            == 404
        )
