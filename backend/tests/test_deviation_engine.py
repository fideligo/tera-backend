"""Unit tests for the deviation engine and the eligibility grader.

Pure functions, no database, so the rules can be checked directly rather than inferred from
API behaviour.
"""

from __future__ import annotations

from pathlib import Path

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


def test_baseline_refuses_fewer_sessions_than_configured(deviation_settings) -> None:
    """The rule, against the configured minimum rather than a number written twice.

    This asserted `at least 3` — BUILD_SPEC 4.3's figure, and the value
    `min_calibration_sessions` held when it was written. The setting has since been lowered to 1,
    so the assertion failed on a threshold change rather than on a behaviour change. Testing it
    against the setting keeps the *rule* covered whatever the figure is, which is the same shape
    the eligibility band uses; `test_the_configured_minimum_is_visible` below is what makes a
    change to the figure itself deliberate.
    """
    # Against an explicit setting rather than the ambient one, so the rule is exercised in this
    # build whatever the configured figure happens to be. A test that skipped itself when the
    # minimum was lowered would stop covering the rule at exactly the moment the rule got looser.
    strict = deviation_settings.model_copy(update={"min_calibration_sessions": 3})

    with pytest.raises(ValueError, match="at least 3"):
        compute_baseline([250.0, 252.0], strict)

    # And it is a threshold, not a constant: the same call passes once the setting allows it.
    assert compute_baseline([250.0, 252.0], deviation_settings).n_sessions == 2


def test_the_configured_minimum_is_visible(deviation_settings) -> None:
    """BUILD_SPEC 4.3 says "at least three accepted calibration sessions".

    It is 1 in this build. That is a real relaxation of the spec figure and not a typo to correct
    in passing: fewer sessions means a baseline whose spread is estimated from almost nothing, and
    every later deviation is measured in units of that spread. Asserted so the gap between the
    spec and the build is stated somewhere rather than being noticed by whoever next reads
    `compute_baseline`'s docstring, which still says three.
    """
    assert deviation_settings.min_calibration_sessions == 1


def test_one_session_yields_a_provisional_baseline(deviation_settings) -> None:
    """**This used to refuse, and refusing was the wrong answer to a correct observation.**

    A single value genuinely has no sample standard deviation. But `min_calibration_sessions` is 1
    because single-point calibration is the product — the patient is told to take one cuff reading
    — and the estimate path never needed a spread: `pressure_estimate` fixes the intercept from one
    anchor and takes the slope from population coefficients (invariant 1). Only the deviation
    engine wanted an SD, and its absence was blocking a mmHg estimate that did not depend on it.

    The ML reference's `classify_trend` states the fallback: "a fixed floor at the bottom of that
    band until enough baseline sessions exist to estimate the between-session SD properly, then
    2 sigma of THAT."
    """
    baseline = compute_baseline([250.0], deviation_settings)

    assert baseline.mean_ms == pytest.approx(250.0)
    assert baseline.n_sessions == 1
    assert baseline.sd_is_provisional is True


def test_the_provisional_spread_reproduces_the_reference_floor(deviation_settings) -> None:
    """`k * sd` must equal the clinically meaningful minimum, to the millisecond.

    That is the whole justification for the number: it is not a guess at this patient's spread, it
    is the reference's fixed threshold expressed as the sigma that produces it.
    """
    baseline = compute_baseline([250.0], deviation_settings)
    threshold_ms = deviation_settings.deviation_k * baseline.sd_ms

    assert threshold_ms == pytest.approx(deviation_settings.trend_min_delta_ms)

    # And it behaves as a threshold: just inside is stable, just outside is a deviation.
    inside, _ = classify_direction(250.0 - 9.0, baseline, deviation_settings.deviation_k)
    outside, _ = classify_direction(250.0 - 11.0, baseline, deviation_settings.deviation_k)
    assert inside is TrendDirection.STABLE
    assert outside is TrendDirection.INCREASE


def test_the_provisional_spread_is_not_the_beat_to_beat_scatter(deviation_settings) -> None:
    """The substitution the reference explicitly forbids.

    "Do NOT use the within-session beat-to-beat SD here. That is a different variance." Per-beat
    scatter is 4-7 ms on a good capture and our own ceiling admits up to 45; either would produce
    a threshold that silently discards the bottom of the 10-50 ms clinically meaningful band.

    Pinned as a relationship rather than a number: the provisional sigma comes from the clinical
    floor and from nothing measured inside a recording.
    """
    baseline = compute_baseline([250.0], deviation_settings)
    assert baseline.sd_ms == pytest.approx(deviation_settings.trend_min_delta_ms / 2.0)


def test_a_real_spread_is_used_as_soon_as_there_is_one(deviation_settings) -> None:
    """The fallback is a floor for the first session, not a permanent substitute."""
    baseline = compute_baseline([246.0, 250.0, 254.0], deviation_settings)
    assert baseline.sd_is_provisional is False
    assert baseline.sd_ms == pytest.approx(4.0)


def test_no_sessions_is_still_refused(deviation_settings) -> None:
    """One is a policy; none is nothing to anchor to."""
    with pytest.raises(ValueError):
        compute_baseline([], deviation_settings)


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


def test_lowering_the_beat_floor_does_not_raise_the_score(deviation_settings) -> None:
    """The saturation point is an absolute count, not a multiple of the minimum.

    `saturation` was `min_usable_beats * 2.0`. Dropping the floor from 30 to 12 would have moved
    the saturation point from 60 beats to 24, so a 24-beat capture would have scored full marks on
    the beat term where it previously scored 0.4. Lowering a gate is a decision about what to
    accept; it must not silently become a decision to describe what it accepts more confidently.
    """
    quality = {"snr_db": 16.0, "motion_index": 0.05, "dropped_frame_pct": 1.0}

    at_old_floor = compute_confidence(
        n_usable_beats=24, quality=quality, min_usable_beats=30, settings=deviation_settings
    )
    at_new_floor = compute_confidence(
        n_usable_beats=24, quality=quality, min_usable_beats=12, settings=deviation_settings
    )
    assert at_old_floor == pytest.approx(at_new_floor)

    # And it is still short of saturation at 24 beats, which is the point.
    full = compute_confidence(
        n_usable_beats=60, quality=quality, min_usable_beats=12, settings=deviation_settings
    )
    assert full > at_new_floor


def test_a_session_at_the_new_floor_scores_low(deviation_settings) -> None:
    """A 12-beat trimmed mean is noisier than a 60-beat one, and the score has to say so.

    This is where the cost of the lower gate is carried. Refusing the capture outright was not the
    conservative choice: it produced no record at all rather than a weak one.
    """
    quality = {"snr_db": 16.0, "motion_index": 0.05, "dropped_frame_pct": 1.0}
    minimal = compute_confidence(
        n_usable_beats=12, quality=quality, min_usable_beats=12, settings=deviation_settings
    )
    full = compute_confidence(
        n_usable_beats=60, quality=quality, min_usable_beats=12, settings=deviation_settings
    )
    assert minimal < full
    assert minimal >= deviation_settings.confidence_floor


def test_the_floor_matches_the_ml_reference() -> None:
    """MIN_PAIRS in the ML team's tera_ptt.py, and the handset's own gate.

    It was 30 here and 12 there, so a capture the signal chain accepted at 12-29 pairs was refused
    on ingest as `insufficient_beats` — a second, stricter gate behind the validated one.
    """
    assert get_settings().deviation.min_usable_beats == 12


def test_the_seed_does_not_restate_the_beat_floor() -> None:
    """The seeded episode must read the default, not copy it.

    `seed_demo.py` wrote a literal `"min_beat_count": 30` into every episode it created, and
    `protocol.min_beat_count` reads `protocol_params` before settings — so the seeded episode
    silently outranked the config and a real 17-beat capture was 422'd by a number in a JSONB
    column. Asserted as source text because running the seeder needs a database and this is really
    a fact about the file: a default belongs in one place.
    """
    source = (
        Path(__file__).resolve().parents[1] / "app" / "cli" / "seed_demo.py"
    ).read_text(encoding="utf-8")

    assert '"min_beat_count": settings.deviation.min_usable_beats' in source
    assert '"min_beat_count": 30' not in source


def test_a_short_capture_still_produces_a_session_ptt(deviation_settings) -> None:
    """Trimming must not fall over on the smaller arrays the lower floor now admits."""
    twelve = [240.0 + (i % 5) for i in range(12)]
    result = trimmed_session_ptt(twelve, deviation_settings)
    assert result.n_considered == 12
    assert 1 <= result.n_retained <= 12
    assert 200.0 < result.value_ms < 280.0


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
        # At or above the 500 Hz target, so the baseline handset is unambiguously qualified and
        # each parametrised override below is the only thing limiting the verdict.
        "accel_rate_hz": 520.0,
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
        # Bands are 200 Hz minimum / 500 Hz target (proposal p.7); see
        # test_device_eligibility_bands.py for the boundaries themselves.
        ({"accel_rate_hz": 250.0}, QualifiedStatus.PROVISIONAL),
        ({"accel_rate_hz": 150.0}, QualifiedStatus.NOT_QUALIFIED),
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
        device_settings, accel_rate_hz=520.0, camera_hw_level=CameraHardwareLevel.LEGACY
    )
    assert verdict.status is QualifiedStatus.NOT_QUALIFIED


def test_every_finding_shows_its_numbers(device_settings) -> None:
    """BUILD_SPEC 5.5 renders the measured value and what it means, never a bare pass/fail."""
    verdict = _grade(device_settings, accel_rate_hz=250.0)

    assert len(verdict.findings) == 6
    for finding in verdict.findings:
        assert finding.measured
        assert finding.threshold
        assert len(finding.explanation) > 30

    limiting = verdict.limiting_findings
    assert any("Accelerometer" in f.measurement for f in limiting)
