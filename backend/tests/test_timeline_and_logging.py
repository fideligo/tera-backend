"""Timeline record separation, and the no-clinical-content rule for logs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from itertools import combinations

import pytest
from fastapi.testclient import TestClient

from app.logging_config import DENIED_FIELDS, REDACTED, RedactingJsonFormatter, is_denied
from app.models import RejectionReason, SessionStatus
from app.schemas.timeline import (
    DISJOINT_RECORD_MODELS,
    EVENT_FIELDS,
    SESSION_LINK_FIELDS,
    STRUCTURAL_FIELDS,
    TimelineCuffReading,
    TimelineTrendEstimate,
)
from tests.conftest import make_session_payload, post_session
from tests.helpers import establish_calibration, find_leaked_markers

#: Contains non-hex characters, so it cannot collide with a logged identifier.
MARKER_SYMPTOM_TEXT = "unmistakable-symptom-text-marker"


# --------------------------------------------------------------------------- timeline types


@pytest.mark.invariant
def test_timeline_returns_estimates_and_readings_as_distinct_types(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """BUILD_SPEC 4.2 — distinct types with distinct field sets.

    Checked two ways: on the models, so the guarantee is structural, and on a real response, so
    it is what a client actually receives.
    """
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    assert (
        post_session(
            client, auth, make_session_payload(episode=episode, device_profile=device_profile)
        ).status_code
        == 201
    )
    assert (
        post_session(
            client,
            auth,
            make_session_payload(
                episode=episode,
                device_profile=device_profile,
                status=SessionStatus.REJECTED,
                rejection_reason=RejectionReason.EXCESSIVE_MOTION.value,
                ptt_ms=[],
                n_beats=10,
            ),
        ).status_code
        == 201
    )

    timeline = client.get(f"/v1/episodes/{episode.id}/timeline", headers=auth).json()
    by_type: dict[str, dict] = {}
    for item in timeline["items"]:
        by_type.setdefault(item["record_type"], item)

    assert {"cuff_reading", "trend_estimate", "rejected_session"} <= set(by_type)

    estimate = by_type["trend_estimate"]
    reading = by_type["cuff_reading"]

    shared = set(estimate) & set(reading)
    assert shared <= STRUCTURAL_FIELDS, (
        f"estimate and cuff reading share non-structural field(s) {shared - STRUCTURAL_FIELDS}. "
        f"A client could render one as the other."
    )

    # The fields that matter are on exactly one of them.
    assert {"systolic_mmhg", "diastolic_mmhg", "unit"} <= set(reading)
    assert not {"systolic_mmhg", "diastolic_mmhg", "unit"} & set(estimate)
    assert {"direction", "magnitude_sd", "estimate_badge"} <= set(estimate)
    assert not {"direction", "magnitude_sd", "estimate_badge"} & set(reading)

    # The badges differ in name as well as value, so neither can be swapped for the other.
    assert reading["cuff_badge"] != estimate["estimate_badge"]


@pytest.mark.invariant
def test_timeline_models_are_pairwise_disjoint() -> None:
    """No two timeline record types share a field outside the documented allowances."""
    allowed = STRUCTURAL_FIELDS | SESSION_LINK_FIELDS | EVENT_FIELDS

    for left, right in combinations(DISJOINT_RECORD_MODELS, 2):
        shared = set(left.model_fields) & set(right.model_fields)
        assert shared <= allowed, (
            f"{left.__name__} and {right.__name__} share {sorted(shared - allowed)}"
        )


@pytest.mark.invariant
def test_estimate_badge_is_not_optional() -> None:
    """The badge travels with the data and cannot be omitted or changed.

    BUILD_SPEC 5.2: "it is not dismissible". Typed as a Literal, so a value other than the
    badge is a validation error rather than a UI decision.
    """
    from app.services import language

    field = TimelineTrendEstimate.model_fields["estimate_badge"]
    assert field.default == language.ESTIMATE_BADGE

    with pytest.raises(Exception):
        TimelineTrendEstimate(
            id="00000000-0000-0000-0000-000000000001",
            occurred_at=datetime.now(tz=timezone.utc),
            session_id="00000000-0000-0000-0000-000000000002",
            calibration_id="00000000-0000-0000-0000-000000000003",
            direction="stable",
            magnitude_sd=0.4,
            confidence=0.8,
            deviation_state="none",
            interpretation="within your usual range",
            estimate_badge="Blood pressure reading",
            synthetic=False,
        )


@pytest.mark.invariant
def test_cuff_reading_states_its_unit() -> None:
    """A measurement carries its unit; an estimate has no unit to carry."""
    assert TimelineCuffReading.model_fields["unit"].default == "mmHg"
    assert "unit" not in TimelineTrendEstimate.model_fields


def test_timeline_is_newest_first(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)
    now = datetime.now(tz=timezone.utc)
    for hours in (5, 1, 3):
        post_session(
            client,
            auth,
            make_session_payload(
                episode=episode,
                device_profile=device_profile,
                started_at=now - timedelta(hours=hours),
            ),
        )

    items = client.get(f"/v1/episodes/{episode.id}/timeline", headers=auth).json()["items"]
    timestamps = [item["occurred_at"] for item in items]
    assert timestamps == sorted(timestamps, reverse=True)


# --------------------------------------------------------------------------- logging


class _RecordCollector(logging.Handler):
    """Collect every record the app emits.

    Used instead of ``caplog`` because ``configure_logging`` clears the root handlers when the
    app starts up, which removes pytest's capture handler along with everything else.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.invariant
def test_logs_contain_no_clinical_values(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """Exercise the ingest and cuff paths, then inspect everything they logged.

    BUILD_SPEC 4.5 allows session id, device profile id, model version, rates, gate outcome and
    timings. It forbids pressure values, PTT values, symptom text and medication detail.
    """
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    collector = _RecordCollector()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(collector)
    root.setLevel(logging.DEBUG)
    try:
        payload = make_session_payload(
            episode=episode, device_profile=device_profile, ptt_target_ms=247.0
        )
        assert post_session(client, auth, payload).status_code == 201

        taken_at = datetime.now(tz=timezone.utc)
        assert (
            client.post(
                "/v1/cuff-readings",
                headers=auth,
                json={
                    "episode_id": str(episode.id),
                    "systolic_mmhg": 173,
                    "diastolic_mmhg": 109,
                    "pulse_bpm": 91,
                    "source": "manual_entry",
                    "taken_at": taken_at.isoformat(),
                    "user_confirmed_at": taken_at.isoformat(),
                },
            ).status_code
            == 201
        )

        assert (
            client.post(
                "/v1/events",
                headers=auth,
                json={
                    "episode_id": str(episode.id),
                    "event_type": "symptom",
                    "occurred_at": taken_at.isoformat(),
                    "payload": {"symptom": MARKER_SYMPTOM_TEXT},
                },
            ).status_code
            == 201
        )
    finally:
        root.removeHandler(collector)
        root.setLevel(previous_level)

    formatter = RedactingJsonFormatter()
    rendered = "\n".join(formatter.format(record) for record in collector.records)

    assert rendered, "nothing was logged, so this test proved nothing"

    # The distinctive values submitted above must appear nowhere in the logs. Checked
    # structurally rather than by substring: a three-digit marker will eventually turn up
    # inside a logged UUID, which is a coincidence, not a disclosure.
    leaks = find_leaked_markers(rendered, ("173", "109", "247.0", MARKER_SYMPTOM_TEXT))
    assert not leaks, f"log output contains clinical value(s): {leaks}"

    for term in ("systolic", "diastolic", "mmhg", "ptt_ms", "symptom_text"):
        assert term not in rendered.lower(), f"log output mentions '{term}'"

    # And the permitted context is genuinely there, so redaction has not simply emptied it.
    assert payload["session_id"] in rendered
    assert "test-1.0.0" in rendered
    assert "gate_outcome" in rendered


@pytest.mark.invariant
def test_formatter_redacts_denied_fields_even_when_passed_deliberately() -> None:
    """A developer who logs a pressure value gets [redacted], not a leak."""
    formatter = RedactingJsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="something_happened", args=(), exc_info=None,
    )
    record.systolic_mmhg = 173
    record.ptt_ms = [251.0, 249.0]
    record.patient_systolic = 180
    record.nested = {"symptom_text": "chest pain", "session_id": "keep-me"}
    record.session_id = "keep-me-too"

    rendered = json.loads(formatter.format(record))

    assert rendered["systolic_mmhg"] == REDACTED
    assert rendered["ptt_ms"] == REDACTED
    assert rendered["patient_systolic"] == REDACTED
    assert rendered["nested"]["symptom_text"] == REDACTED
    assert rendered["nested"]["session_id"] == "keep-me"
    assert rendered["session_id"] == "keep-me-too"
    assert "173" not in json.dumps(rendered)
    assert "chest pain" not in json.dumps(rendered)


def test_deny_list_covers_the_obvious_names() -> None:
    for name in ("systolic", "diastolic", "ptt_ms", "medication", "password", "token"):
        assert name in DENIED_FIELDS
    for variant in ("patient_systolic_mmhg", "SYSTOLIC", "session_ptt_ms", "access_token"):
        assert is_denied(variant), variant
    for allowed in ("session_id", "device_profile_id", "model_version", "camera_fps"):
        assert not is_denied(allowed), allowed
