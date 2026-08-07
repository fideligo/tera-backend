"""Invariant 3 — rejected sessions are retained, never discarded.

"Status and rejection reason are persisted and must be mutually consistent. The clinician
summary reports them."
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.models import MeasurementSession, RejectionReason, SessionStatus
from tests.conftest import make_session_payload, post_session


@pytest.mark.invariant
def test_rejected_session_requires_reason(db, episode, device_profile) -> None:
    """The database refuses a rejected session with no reason."""
    with pytest.raises(sa.exc.IntegrityError) as excinfo:
        db.add(
            MeasurementSession(
                id=uuid.uuid4(),
                episode_id=episode.id,
                device_profile_id=device_profile.id,
                model_version="direct",
                started_at=datetime.now(tz=timezone.utc),
                posture="seated",
                status=SessionStatus.REJECTED,
                rejection_reason=None,
                n_beats_total=10,
                n_beats_usable=0,
                ptt_ms=[],
                quality={},
            )
        )
        db.flush()

    assert "ck_session_rejection_reason_matches_status" in str(excinfo.value)
    db.rollback()


@pytest.mark.invariant
def test_accepted_session_must_not_have_reason(db, episode, device_profile) -> None:
    """The converse: a completed session with a rejection reason is equally refused."""
    with pytest.raises(sa.exc.IntegrityError) as excinfo:
        db.add(
            MeasurementSession(
                id=uuid.uuid4(),
                episode_id=episode.id,
                device_profile_id=device_profile.id,
                model_version="direct",
                started_at=datetime.now(tz=timezone.utc),
                posture="seated",
                status=SessionStatus.COMPLETED,
                rejection_reason=RejectionReason.EXCESSIVE_MOTION,
                n_beats_total=50,
                n_beats_usable=50,
                ptt_ms=[250.0] * 50,
                quality={},
            )
        )
        db.flush()

    assert "ck_session_rejection_reason_matches_status" in str(excinfo.value)
    db.rollback()


@pytest.mark.invariant
def test_api_rejects_inconsistent_status_and_reason(
    client: TestClient, auth, episode, device_profile
) -> None:
    """The same consistency rule holds at the API boundary, as a 422 not a 500."""
    payload = make_session_payload(
        episode=episode, device_profile=device_profile, status=SessionStatus.REJECTED
    )
    payload["rejection_reason"] = None
    assert post_session(client, auth, payload).status_code == 422

    payload = make_session_payload(episode=episode, device_profile=device_profile)
    payload["rejection_reason"] = RejectionReason.EXCESSIVE_MOTION.value
    assert post_session(client, auth, payload).status_code == 422


@pytest.mark.invariant
def test_rejected_session_is_persisted_not_discarded(
    client: TestClient, auth, db, episode, device_profile
) -> None:
    """A rejected session comes back 201 and is on the record afterwards."""
    payload = make_session_payload(
        episode=episode,
        device_profile=device_profile,
        status=SessionStatus.REJECTED,
        rejection_reason=RejectionReason.EXCESSIVE_MOTION.value,
        ptt_ms=[250.0, 252.0, 248.0],
        n_beats=20,
    )
    response = post_session(client, auth, payload)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["trend"] is None, "a rejected session must not produce an estimate"
    assert body["rejection"]["reason"] == "excessive_motion"
    assert body["rejection"]["message"], "the patient is told why, in plain language"

    stored = db.get(MeasurementSession, uuid.UUID(payload["session_id"]))
    assert stored is not None, "the rejected session was discarded"
    assert stored.rejection_reason is RejectionReason.EXCESSIVE_MOTION


@pytest.mark.invariant
def test_rejected_session_appears_in_timeline(
    client: TestClient, auth, episode, device_profile
) -> None:
    """Present, visible, never hidden (BUILD_SPEC 5.2)."""
    payload = make_session_payload(
        episode=episode,
        device_profile=device_profile,
        status=SessionStatus.REJECTED,
        rejection_reason=RejectionReason.POOR_SIGNAL_QUALITY.value,
        ptt_ms=[],
        n_beats=14,
    )
    assert post_session(client, auth, payload).status_code == 201

    timeline = client.get(f"/v1/episodes/{episode.id}/timeline", headers=auth).json()
    rejected = [i for i in timeline["items"] if i["record_type"] == "rejected_session"]

    assert len(rejected) == 1
    assert rejected[0]["rejection_reason"] == "poor_signal_quality"
    assert rejected[0]["reason_text"]
    assert rejected[0]["retry_available"] is True


@pytest.mark.invariant
def test_rejected_sessions_appear_in_summary(
    client: TestClient, auth, clinician_auth, episode, device_profile
) -> None:
    """"The clinician summary reports them" — with reasons and a per-reason breakdown."""
    reasons = (
        RejectionReason.EXCESSIVE_MOTION,
        RejectionReason.POOR_SIGNAL_QUALITY,
        RejectionReason.USER_ABORTED,
    )
    for reason in reasons:
        payload = make_session_payload(
            episode=episode,
            device_profile=device_profile,
            status=SessionStatus.REJECTED,
            rejection_reason=reason.value,
            ptt_ms=[],
            n_beats=12,
        )
        assert post_session(client, auth, payload).status_code == 201

    summary = client.get(
        f"/v1/episodes/{episode.id}/summary", headers=clinician_auth
    ).json()

    assert len(summary["rejected_sessions"]) == 3
    reported = {row["rejection_reason"] for row in summary["rejected_sessions"]}
    assert reported == {r.value for r in reasons}
    assert summary["session_yield"]["sessions_rejected"] == 3
    assert summary["session_yield"]["rejections_by_reason"] == {
        r.value: 1 for r in reasons
    }
