"""Invariant 1 — no mmHg from SCG-PPG, ever.

"The ``trend_estimate`` entity has no systolic or diastolic column and no API response derived
from SCG-PPG may contain a pressure value."
"""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.schemas.session import TrendEstimateOut
from app.schemas.timeline import TimelineTrendEstimate
from tests.conftest import make_session_payload, post_session

#: Any column or field name containing one of these would be a pressure value by another name.
PRESSURE_TOKENS = (
    "systolic",
    "diastolic",
    "mmhg",
    "mm_hg",
    "pressure",
    "bp_",
    "_bp",
)


@pytest.mark.invariant
def test_trend_estimate_has_no_pressure_column(db) -> None:
    """Introspect the live schema and assert the absence.

    This reads the database, not the model file, so it also catches a column added by a
    migration that was never reflected back into the SQLAlchemy model.
    """
    inspector = sa.inspect(db.get_bind())
    columns = {column["name"].lower() for column in inspector.get_columns("trend_estimate")}

    assert columns, "trend_estimate has no columns — did the migration run?"

    offending = {
        name for name in columns if any(token in name for token in PRESSURE_TOKENS)
    }
    assert not offending, (
        f"trend_estimate has pressure-like column(s) {sorted(offending)}. Invariant 1: an "
        f"estimate is a direction plus a magnitude in baseline standard deviations. Only "
        f"cuff_reading holds mmHg."
    )


@pytest.mark.invariant
def test_estimate_response_models_have_no_pressure_fields() -> None:
    """The API boundary carries the same prohibition as the schema."""
    for model in (TrendEstimateOut, TimelineTrendEstimate):
        fields = {name.lower() for name in model.model_fields}
        offending = {
            name for name in fields if any(token in name for token in PRESSURE_TOKENS)
        }
        assert not offending, f"{model.__name__} exposes pressure-like field(s) {offending}"


@pytest.mark.invariant
def test_no_pressure_value_in_any_estimate_response(
    client: TestClient, auth, episode, device_profile, cuff_reading, db
) -> None:
    """End to end: submit a session, and prove the response carries no pressure value.

    The check is on the serialised JSON, not on the model, because that is what a client
    actually receives.
    """
    from tests.helpers import establish_calibration

    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    response = post_session(
        client,
        auth,
        make_session_payload(episode=episode, device_profile=device_profile),
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["trend"] is not None, "expected an estimate once a calibration is in force"

    _assert_no_pressure_anywhere(body["trend"])

    # And again from the timeline, which is the other place an estimate surfaces.
    timeline = client.get(f"/v1/episodes/{episode.id}/timeline", headers=auth).json()
    for item in timeline["items"]:
        if item["record_type"] == "trend_estimate":
            _assert_no_pressure_anywhere(item)


def _assert_no_pressure_anywhere(payload: dict) -> None:
    """No pressure-like key, and no value that reads like a pressure."""
    for key in payload:
        assert not any(token in key.lower() for token in PRESSURE_TOKENS), (
            f"estimate response contains pressure-like key '{key}'"
        )

    serialised = json.dumps(payload).lower()
    # "blood pressure" appears legitimately inside the badge and the magnitude notice, both of
    # which exist to say the value is *not* one. Strip them before the numeric check.
    for allowed in (
        "not a blood-pressure reading",
        "it is not a blood pressure and does not convert to one",
    ):
        serialised = serialised.replace(allowed, "")

    assert "mmhg" not in serialised, "estimate response mentions mmHg"
    assert "systolic" not in serialised
    assert "diastolic" not in serialised
