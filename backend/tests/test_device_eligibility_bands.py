"""Device eligibility bands, and the thing PROVISIONAL must never become.

The bands come from the proposal (page 7): minimum 200 Hz, target 500 Hz. Android caps sensor
delivery at 200 Hz without HIGH_SAMPLING_RATE_SENSORS, so **most real handsets land in the
provisional band** — which makes the question "what does provisional actually restrict?" the one
worth pinning down. The answer is nothing, and the answer has to stay nothing, because a status
that quietly gated estimates would be a second, undocumented eligibility rule.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import DeviceEligibilitySettings
from app.models.enums import CameraHardwareLevel, QualifiedStatus, TimestampSource
from app.services.eligibility import evaluate_device

pytestmark = pytest.mark.invariant


def _verdict(accel_rate_hz: float) -> QualifiedStatus:
    """Grade a handset that is unremarkable in every respect except its sensor rate."""
    return evaluate_device(
        accel_rate_hz=accel_rate_hz,
        camera_fps=60.0,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.0,
        settings=DeviceEligibilitySettings(),
    ).status


@pytest.mark.parametrize(
    ("accel_rate_hz", "expected"),
    [
        (99.0, QualifiedStatus.NOT_QUALIFIED),
        (199.9, QualifiedStatus.NOT_QUALIFIED),
        # The boundaries are inclusive at the bottom of each band.
        (200.0, QualifiedStatus.PROVISIONAL),
        (204.8, QualifiedStatus.PROVISIONAL),
        (499.9, QualifiedStatus.PROVISIONAL),
        (500.0, QualifiedStatus.QUALIFIED),
        (1000.0, QualifiedStatus.QUALIFIED),
    ],
)
def test_accelerometer_bands_match_the_proposal(
    accel_rate_hz: float, expected: QualifiedStatus
) -> None:
    """Below 200 Hz excluded, 200–500 provisional, 500 Hz and up qualified."""
    assert _verdict(accel_rate_hz) == expected


def test_the_configured_bands_are_the_proposal_figures() -> None:
    """The numbers themselves, not just the behaviour.

    Invariant 10: thresholds are configuration. A test on behaviour alone would pass just as
    happily if someone moved the floor to 150 Hz.
    """
    settings = DeviceEligibilitySettings()

    assert settings.accel_rate_provisional_hz == 200.0
    assert settings.accel_rate_qualified_hz == 500.0


def test_provisional_explains_itself_without_a_lookup() -> None:
    """The band most handsets land in must say what it does and does not mean.

    A bare "PROVISIONAL" invites the reader to supply their own meaning, and the meaning they
    supply is usually worse than the truth.
    """
    verdict = evaluate_device(
        accel_rate_hz=204.8,
        camera_fps=60.0,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.0,
        settings=DeviceEligibilitySettings(),
    )
    assert verdict.status is QualifiedStatus.PROVISIONAL

    explanation = " ".join(f.explanation for f in verdict.limiting_findings).lower()

    # It says the handset is usable and nothing is withheld...
    assert "cleared for use" in explanation
    assert "nothing is restricted" in explanation
    # ...that this is the ordinary outcome rather than a defect...
    assert "usual result rather than a fault" in explanation
    # ...and that the real consequence is more repeat spot checks, not weaker estimates.
    assert "repeat spot checks" in explanation
    assert "never as a less trustworthy estimate" in explanation


def test_provisional_status_gates_nothing(
    client, patient, episode, auth, db
) -> None:
    """A provisionally-qualified handset produces sessions exactly like a qualified one.

    This is the test the band exists to be protected by. `qualified_status` is computed and
    stored and rendered; it is never consulted before accepting a session or producing an
    estimate. If someone later makes it a gate, the eligibility rule would live in two places —
    one documented, one not — and a patient on the most common class of Android handset would
    silently stop getting estimates.
    """
    from app.models import DeviceProfile

    profiles: dict[QualifiedStatus, DeviceProfile] = {}
    for status, rate in (
        (QualifiedStatus.QUALIFIED, 500.0),
        (QualifiedStatus.PROVISIONAL, 204.8),
    ):
        row = DeviceProfile(
            patient_id=patient.id,
            model=f"Handset {status.value}",
            os_version="Android 14",
            accel_rate_hz=rate,
            camera_fps=60.0,
            camera_hw_level=CameraHardwareLevel.FULL,
            manual_sensor=True,
            timestamp_source=TimestampSource.REALTIME,
            clock_offset_sd_ms=1.0,
            qualified_status=status,
        )
        db.add(row)
        profiles[status] = row
    db.commit()

    outcomes: dict[QualifiedStatus, int] = {}
    for status, profile in profiles.items():
        nonce = client.post("/v1/sessions/nonce", headers=auth).json()["nonce"]
        session_id = str(uuid.uuid4())
        response = client.post(
            "/v1/sessions",
            headers={
                **auth,
                "X-Session-Nonce": nonce,
                "Idempotency-Key": session_id,
            },
            json={
                "session_id": session_id,
                "episode_id": str(episode.id),
                "device_profile_id": str(profile.id),
                "model_version": "test-1.0",
                "started_at": "2026-08-09T09:00:00Z",
                "posture": "seated",
                "status": "completed",
                "n_beats_total": 40,
                "n_beats_usable": 38,
                "ptt_ms": [230.0] * 38,
                "quality": {
                    "accel_rate_hz": profile.accel_rate_hz,
                    "camera_fps": 60.0,
                    "dropped_frame_pct": 1.0,
                    "snr_db": 12.0,
                    "motion_index": 0.1,
                },
                "synthetic": False,
            },
        )
        outcomes[status] = response.status_code

    # Equality alone would be satisfied by both being rejected, which is the vacuous version of
    # this test. Pin the success explicitly: both must be accepted, and identically.
    assert outcomes[QualifiedStatus.QUALIFIED] == 201, (
        f"the control case must succeed or this test proves nothing; got {outcomes}"
    )
    assert outcomes[QualifiedStatus.PROVISIONAL] == outcomes[QualifiedStatus.QUALIFIED], (
        "a provisional handset must be treated exactly like a qualified one; "
        f"got {outcomes}"
    )


def test_seeded_device_profiles_are_labelled_as_illustrative(client, auth, db, patient):
    """A seeded profile must not read as a hardware benchmark.

    Invariant 9 names device benchmarks specifically. The generic synthetic badge says the row is
    not a measurement *from a person*, which is the wrong reassurance for a row whose content is
    "204.8 Hz" — that reads as something somebody measured on a bench.
    """
    from app.models import DeviceProfile
    from app.services import language

    row = DeviceProfile(
        patient_id=patient.id,
        model="Synthetic Reference Handset (demo)",
        os_version="Android 14",
        accel_rate_hz=204.8,
        camera_fps=60.4,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.4,
        qualified_status=QualifiedStatus.PROVISIONAL,
        synthetic=True,
    )
    db.add(row)
    db.commit()

    body = client.get(f"/v1/device-profiles/{row.id}", headers=auth).json()

    assert body["synthetic"] is True
    assert body["synthetic_notice"] == language.SYNTHETIC_DEVICE_PROFILE_NOTICE
    notice = body["synthetic_notice"].lower()
    assert "not measured performance" in notice
    assert "illustrative" in notice
