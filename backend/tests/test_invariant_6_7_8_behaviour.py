"""Invariants 6, 7 and 8 — what the system says, when it escalates, and red flags.

6. The system never diagnoses and never advises on medication.
7. Bias toward escalation: where anything is ambiguous, request a cuff reading — never produce
   an estimate.
8. Red-flag symptoms terminate the session with an instruction to seek emergency care.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.models import RejectionReason, SessionStatus
from app.services import language
from tests.conftest import make_session_payload, post_session
from tests.helpers import establish_calibration


# --------------------------------------------------------------------------- invariant 6


@pytest.mark.invariant
def test_no_diagnostic_or_medication_advice_language() -> None:
    """Every user-facing string is checked against the deny-list.

    ``language.all_user_facing_strings()`` enumerates the module, so a string added there is
    covered automatically rather than needing this test updated.
    """
    offenders: dict[str, list[str]] = {}
    for name, text in language.all_user_facing_strings().items():
        found = language.find_forbidden_language(text)
        if found:
            offenders[name] = found

    assert not offenders, (
        f"deny-listed language in user-facing strings: {offenders}. Invariant 6: no diagnosis, "
        f"no medication advice, no clinical reassurance."
    )


@pytest.mark.invariant
def test_api_responses_contain_no_advice_language(
    client: TestClient, auth, clinician_auth, db, episode, device_profile, cuff_reading
) -> None:
    """The same check against real response bodies, not just the string constants."""
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    assert (
        post_session(
            client, auth, make_session_payload(episode=episode, device_profile=device_profile)
        ).status_code
        == 201
    )

    bodies = [
        client.get(f"/v1/episodes/{episode.id}/timeline", headers=auth).text,
        client.get(f"/v1/episodes/{episode.id}/summary", headers=clinician_auth).text,
        client.get("/v1/episodes", headers=auth).text,
    ]

    for body in bodies:
        found = language.find_forbidden_language(body)
        assert not found, f"deny-listed language in a response body: {found}"


@pytest.mark.invariant
def test_summary_reports_medication_as_counts_not_judgement(
    client: TestClient, clinician_auth, episode
) -> None:
    """The medication section is a factual log, with no adherence verdict."""
    summary = client.get(
        f"/v1/episodes/{episode.id}/summary", headers=clinician_auth
    ).json()

    log = summary["medication_log"]
    assert set(log) == {
        "events_logged",
        "days_with_a_log",
        "episode_days_elapsed",
        "first_logged_at",
        "last_logged_at",
    }
    # No score, grade, verdict or recommendation anywhere in the document.
    serialised = json.dumps(summary).lower()
    for word in ("adherent", "non-adherent", "compliance", "recommend", "should take"):
        assert word not in serialised, f"summary contains a judgement word: {word}"


# --------------------------------------------------------------------------- invariant 7


@pytest.mark.invariant
def test_missing_calibration_yields_no_estimate_and_requests_cuff(
    client: TestClient, auth, episode, device_profile
) -> None:
    """With no calibration in force, the session is kept but no estimate is produced."""
    response = post_session(
        client, auth, make_session_payload(episode=episode, device_profile=device_profile)
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "completed", "the capture is still on the record"
    assert body["trend"] is None, "an estimate without a baseline is not interpretable"
    assert body["action"]["kind"] == "cuff_reading_requested"


@pytest.mark.invariant
def test_single_deviating_session_does_not_request_cuff(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """"A single deviating session never triggers a cuff request" (BUILD_SPEC 4.3).

    Baseline is mean 250, sd 4, k=2, so the threshold is 8 ms. A session at 230 ms is five
    standard deviations short — unambiguously a deviation — and it still only asks for a
    repeat.
    """
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    response = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode, device_profile=device_profile, ptt_target_ms=230.0
        ),
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["trend"]["direction"] == "increase", "shorter PTT means increase"
    assert body["trend"]["deviation_state"] == "possible"
    assert body["action"]["kind"] == "repeat_session_suggested"
    assert body["action"]["kind"] != "cuff_reading_requested"


@pytest.mark.invariant
def test_persistent_deviation_requests_cuff(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """A repeat inside the window makes it persistent, and that does request a cuff."""
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    now = datetime.now(tz=timezone.utc)

    first = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            ptt_target_ms=230.0,
            started_at=now - timedelta(hours=6),
        ),
    ).json()
    assert first["trend"]["deviation_state"] == "possible"

    second = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            ptt_target_ms=229.0,
            started_at=now - timedelta(hours=1),
        ),
    ).json()

    assert second["trend"]["deviation_state"] == "persistent"
    assert second["action"]["kind"] == "cuff_reading_requested"


@pytest.mark.invariant
def test_deviation_outside_the_window_is_not_persistent(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """Two deviations far apart are not a persistent deviation.

    The window exists because sessions a week apart describe different physiological states.
    """
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    now = datetime.now(tz=timezone.utc)

    post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            ptt_target_ms=230.0,
            started_at=now - timedelta(days=10),
        ),
    )
    second = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            ptt_target_ms=230.0,
            started_at=now - timedelta(hours=1),
        ),
    ).json()

    assert second["trend"]["deviation_state"] == "possible"
    assert second["action"]["kind"] == "repeat_session_suggested"


@pytest.mark.invariant
def test_opposite_direction_repeat_is_not_persistent(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """High then low is instability, not a trend.

    Requesting a cuff on that pairing would train the patient to ignore the request.
    """
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    now = datetime.now(tz=timezone.utc)

    post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            ptt_target_ms=230.0,
            started_at=now - timedelta(hours=6),
        ),
    )
    second = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            ptt_target_ms=272.0,
            started_at=now - timedelta(hours=1),
        ),
    ).json()

    assert second["trend"]["direction"] == "decrease"
    assert second["trend"]["deviation_state"] == "possible"


@pytest.mark.invariant
def test_rejected_session_never_produces_an_estimate(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """Even with a calibration in force, a rejected session yields nothing."""
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    body = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            status=SessionStatus.REJECTED,
            rejection_reason=RejectionReason.POOR_SIGNAL_QUALITY.value,
            ptt_ms=[250.0, 251.0],
            n_beats=20,
        ),
    ).json()

    assert body["trend"] is None
    assert body["action"]["kind"] == "cuff_reading_requested"


# --------------------------------------------------------------------------- invariant 8


@pytest.mark.invariant
def test_red_flag_event_returns_emergency_instruction_and_no_estimate(
    client: TestClient, auth, episode
) -> None:
    """A red-flag report yields the emergency instruction and nothing else.

    No measurement is offered and no estimate is displayed.
    """
    response = client.post(
        "/v1/events",
        headers=auth,
        json={
            "episode_id": str(episode.id),
            "event_type": "red_flag",
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            "payload": {"symptom": "chest pain", "red_flag": True},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["emergency_instruction"] == language.ACTION_SEEK_EMERGENCY_CARE
    assert "emergency" in body["emergency_instruction"].lower()
    assert "trend" not in body and "magnitude_sd" not in body
    assert "systolic_mmhg" not in json.dumps(body)


@pytest.mark.invariant
def test_non_red_flag_events_carry_no_instruction(client: TestClient, auth, episode) -> None:
    """A medication or ordinary symptom report gets an acknowledgement, not advice."""
    for event_type in ("medication", "symptom"):
        response = client.post(
            "/v1/events",
            headers=auth,
            json={
                "episode_id": str(episode.id),
                "event_type": event_type,
                "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
                "payload": {"note": "routine"},
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["emergency_instruction"] is None


@pytest.mark.invariant
def test_session_rejected_for_red_flag_routes_to_emergency_care(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """A session terminated by a red flag routes to emergency care, not to a cuff reading."""
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    body = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            status=SessionStatus.REJECTED,
            rejection_reason=RejectionReason.RED_FLAG_REPORTED.value,
            ptt_ms=[],
            n_beats=4,
        ),
    ).json()

    assert body["trend"] is None
    assert body["action"]["kind"] == "seek_emergency_care"
    assert body["action"]["message"] == language.ACTION_SEEK_EMERGENCY_CARE
