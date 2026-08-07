"""Unit tests for the deviation engine and the eligibility grader.

Pure functions, no database, so the rules can be checked directly rather than inferred from
API behaviour.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.models.enums import (
    CameraHardwareLevel,
    DeviationState,
    QualifiedStatus,
    TimestampSource,
    TrendDirection,
)
from app.services.deviation import (
    Baseline,
    classify_direction,
    compute_baseline,
    compute_confidence,
    resolve_deviation_state,
    trimmed_session_ptt,
)
from app.services.eligibility import evaluate_device


@pytest.fixture
def deviation_settings():
    return get_settings().deviation


def test_trimmed_mean_discards_beats_beyond_the_iqr_fence(deviation_settings) -> None:
    """A single misplaced fiducial point must not move the session value."""
    clean = [250.0, 251.0, 249.0, 252.0, 248.0, 250.0, 251.0, 249.0]
    with_outlier = [*clean, 390.0]

    baseline_result = trimmed_session_ptt(clean, deviation_settings)
    outlier_result = trimmed_session_ptt(with_outlier, deviation_settings)

    assert outlier_result.n_retained == len(clean)
    assert outlier_result.n_considered == len(with_outlier)
    assert outlier_result.value_ms == pytest.approx(baseline_result.value_ms, abs=0.5)


def test_trimmed_mean_falls_back_to_plain_mean_below_four_beats(deviation_settings) -> None:
    """A quartile is not defined for fewer than four values."""
    result = trimmed_session_ptt([250.0, 260.0, 240.0], deviation_settings)
    assert result.value_ms == pytest.approx(250.0)
    assert result.n_retained == 3


def test_trimmed_mean_handles_identical_beats(deviation_settings) -> None:
    """A zero-width fence must not leave an empty set."""
    result = trimmed_session_ptt([250.0] * 10, deviation_settings)
    assert result.value_ms == pytest.approx(250.0)
    assert result.n_retained == 10


def test_trimmed_mean_rejects_an_empty_array(deviation_settings) -> None:
    with pytest.raises(ValueError, match="zero beats"):
        trimmed_session_ptt([], deviation_settings)


def test_baseline_requires_three_sessions(deviation_settings) -> None:
    with pytest.raises(ValueError, match="at least 3"):
        compute_baseline([250.0, 252.0], deviation_settings)


def test_baseline_uses_sample_standard_deviation(deviation_settings) -> None:
    """n-1, because the calibration sessions are a sample of day-to-day variation."""
    baseline = compute_baseline([246.0, 250.0, 254.0], deviation_settings)
    assert baseline.mean_ms == pytest.approx(250.0)
    assert baseline.sd_ms == pytest.approx(4.0)
    assert baseline.n_sessions == 3


def test_zero_variance_baseline_is_refused(deviation_settings) -> None:
    """Invariant 7 — no usable baseline means no baseline, not a guessed one."""
    with pytest.raises(ValueError, match="standard deviation is zero"):
        compute_baseline([250.0, 250.0, 250.0], deviation_settings)


def test_shorter_ptt_maps_to_increase() -> None:
    """The sign that is easy to get backwards.

    Shorter transit time means a faster pulse wave, which implies a stiffer artery, which is
    associated with higher pressure.
    """
    baseline = Baseline(mean_ms=250.0, sd_ms=4.0, n_sessions=3)

    direction, magnitude = classify_direction(240.0, baseline, deviation_k=2.0)
    assert direction is TrendDirection.INCREASE
    assert magnitude == pytest.approx(2.5)

    direction, magnitude = classify_direction(260.0, baseline, deviation_k=2.0)
    assert direction is TrendDirection.DECREASE
    assert magnitude == pytest.approx(2.5)


def test_within_k_standard_deviations_is_stable() -> None:
    baseline = Baseline(mean_ms=250.0, sd_ms=4.0, n_sessions=3)

    direction, magnitude = classify_direction(254.0, baseline, deviation_k=2.0)
    assert direction is TrendDirection.STABLE
    assert magnitude == pytest.approx(1.0)


def test_the_threshold_is_inclusive() -> None:
    """Exactly k standard deviations counts as a deviation."""
    baseline = Baseline(mean_ms=250.0, sd_ms=4.0, n_sessions=3)

    at_threshold, _ = classify_direction(242.0, baseline, deviation_k=2.0)
    just_inside, _ = classify_direction(242.1, baseline, deviation_k=2.0)

    assert at_threshold is TrendDirection.INCREASE
    assert just_inside is TrendDirection.STABLE


def test_magnitude_is_in_baseline_units_not_milliseconds() -> None:
    """The same absolute shift means more for a patient with a tighter baseline.

    That is the whole point of expressing it in the patient's own standard deviations.
    """
    tight = Baseline(mean_ms=250.0, sd_ms=2.0, n_sessions=3)
    loose = Baseline(mean_ms=250.0, sd_ms=8.0, n_sessions=3)

    _, tight_magnitude = classify_direction(240.0, tight, deviation_k=2.0)
    _, loose_magnitude = classify_direction(240.0, loose, deviation_k=2.0)

    assert tight_magnitude == pytest.approx(5.0)
    assert loose_magnitude == pytest.approx(1.25)


@pytest.mark.parametrize(
    ("direction", "prior", "expected"),
    [
        (TrendDirection.STABLE, None, DeviationState.NONE),
        (TrendDirection.STABLE, TrendDirection.INCREASE, DeviationState.NONE),
        (TrendDirection.INCREASE, None, DeviationState.POSSIBLE),
        (TrendDirection.INCREASE, TrendDirection.INCREASE, DeviationState.PERSISTENT),
        (TrendDirection.INCREASE, TrendDirection.DECREASE, DeviationState.POSSIBLE),
        (TrendDirection.DECREASE, TrendDirection.DECREASE, DeviationState.PERSISTENT),
    ],
)
def test_persistence_requires_a_repeat_in_the_same_direction(
    direction, prior, expected
) -> None:
    assert resolve_deviation_state(direction=direction, prior_deviating_direction=prior) is expected


def test_confidence_stays_strictly_below_one(deviation_settings) -> None:
    """No response may read as certainty."""
    perfect = compute_confidence(
        n_usable_beats=1000,
        quality={"snr_db": 100.0, "motion_index": 0.0, "dropped_frame_pct": 0.0},
        min_usable_beats=30,
        settings=deviation_settings,
    )
    assert 0.0 < perfect < 1.0
    assert perfect == pytest.approx(deviation_settings.confidence_ceiling)


def test_confidence_takes_the_worst_quality_limb(deviation_settings) -> None:
    """Averaging would let a good SNR hide a capture ruined by motion."""
    good_snr_bad_motion = compute_confidence(
        n_usable_beats=60,
        quality={"snr_db": 20.0, "motion_index": 0.95, "dropped_frame_pct": 0.0},
        min_usable_beats=30,
        settings=deviation_settings,
    )
    all_good = compute_confidence(
        n_usable_beats=60,
        quality={"snr_db": 20.0, "motion_index": 0.05, "dropped_frame_pct": 0.0},
        min_usable_beats=30,
        settings=deviation_settings,
    )
    assert good_snr_bad_motion < all_good


def test_confidence_rises_with_usable_beats(deviation_settings) -> None:
    quality = {"snr_db": 16.0, "motion_index": 0.05, "dropped_frame_pct": 1.0}
    few = compute_confidence(
        n_usable_beats=31, quality=quality, min_usable_beats=30, settings=deviation_settings
    )
    many = compute_confidence(
        n_usable_beats=60, quality=quality, min_usable_beats=30, settings=deviation_settings
    )
    assert many > few


def test_confidence_saturates(deviation_settings) -> None:
    """More beats past the saturation point add nothing."""
    quality = {"snr_db": 16.0, "motion_index": 0.05, "dropped_frame_pct": 1.0}
    saturated = compute_confidence(
        n_usable_beats=60, quality=quality, min_usable_beats=30, settings=deviation_settings
    )
    beyond = compute_confidence(
        n_usable_beats=600, quality=quality, min_usable_beats=30, settings=deviation_settings
    )
    assert saturated == pytest.approx(beyond)


def test_confidence_handles_a_missing_quality_field(deviation_settings) -> None:
    """A missing metric is treated as the worst case, not ignored (invariant 7)."""
    result = compute_confidence(
        n_usable_beats=60, quality={}, min_usable_beats=30, settings=deviation_settings
    )
    assert result == pytest.approx(deviation_settings.confidence_floor + 0.5 * (
        deviation_settings.confidence_ceiling - deviation_settings.confidence_floor
    ), abs=0.01)


# --------------------------------------------------------------------------- eligibility


@pytest.fixture
def device_settings():
    return get_settings().device


def _grade(device_settings, **overrides):
    kwargs = {
        "accel_rate_hz": 220.0,
        "camera_fps": 60.0,
        "camera_hw_level": CameraHardwareLevel.FULL,
        "manual_sensor": True,
        "timestamp_source": TimestampSource.REALTIME,
        "clock_offset_sd_ms": 1.0,
        "settings": device_settings,
    }
    kwargs.update(overrides)
    return evaluate_device(**kwargs)


def test_a_capable_handset_qualifies(device_settings) -> None:
    assert _grade(device_settings).status is QualifiedStatus.QUALIFIED


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"accel_rate_hz": 150.0}, QualifiedStatus.PROVISIONAL),
        ({"accel_rate_hz": 40.0}, QualifiedStatus.NOT_QUALIFIED),
        ({"camera_fps": 45.0}, QualifiedStatus.PROVISIONAL),
        ({"camera_fps": 15.0}, QualifiedStatus.NOT_QUALIFIED),
        ({"camera_hw_level": CameraHardwareLevel.LIMITED}, QualifiedStatus.PROVISIONAL),
        ({"camera_hw_level": CameraHardwareLevel.LEGACY}, QualifiedStatus.NOT_QUALIFIED),
        ({"manual_sensor": False}, QualifiedStatus.PROVISIONAL),
        ({"timestamp_source": TimestampSource.UNKNOWN}, QualifiedStatus.PROVISIONAL),
        ({"clock_offset_sd_ms": 3.0}, QualifiedStatus.PROVISIONAL),
        ({"clock_offset_sd_ms": 25.0}, QualifiedStatus.NOT_QUALIFIED),
    ],
)
def test_each_measurement_can_limit_the_verdict(
    device_settings, overrides, expected
) -> None:
    assert _grade(device_settings, **overrides).status is expected


def test_the_verdict_is_the_worst_finding(device_settings) -> None:
    """A phone is only as capable as its weakest relevant property."""
    verdict = _grade(
        device_settings, accel_rate_hz=400.0, camera_hw_level=CameraHardwareLevel.LEGACY
    )
    assert verdict.status is QualifiedStatus.NOT_QUALIFIED


def test_every_finding_shows_its_numbers(device_settings) -> None:
    """BUILD_SPEC 5.5 renders the measured value and what it means, never a bare pass/fail."""
    verdict = _grade(device_settings, accel_rate_hz=150.0)

    assert len(verdict.findings) == 6
    for finding in verdict.findings:
        assert finding.measured
        assert finding.threshold
        assert len(finding.explanation) > 30

    limiting = verdict.limiting_findings
    assert any("Accelerometer" in f.measurement for f in limiting)
