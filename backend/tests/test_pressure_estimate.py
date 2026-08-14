"""PTT -> mmHg, and the cases where no number is produced.

Invariant 1 changed: the product now estimates blood pressure from pulse transit time after a
single cuff calibration, as Samsung Health Monitor and similar cuffless products do. What did
*not* change is that every number comes from a real measurement through a stated model. These
tests exist to hold that line — most of them assert the estimator **refuses**, because refusing
is the behaviour that keeps an estimate honest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import PressureEstimateSettings
from app.services.pressure_estimate import estimate

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
FRESH = NOW - timedelta(days=2)


def _settings() -> PressureEstimateSettings:
    return PressureEstimateSettings()


def _estimate(**overrides):
    kwargs = {
        "ptt_now_ms": 250.0,
        "baseline_ptt_ms": 250.0,
        "calibration_systolic": 120,
        "calibration_diastolic": 80,
        "calibration_established_at": FRESH,
        "settings": _settings(),
        "now": NOW,
    }
    kwargs.update(overrides)
    return estimate(**kwargs)


@pytest.mark.invariant
def test_at_the_calibration_point_the_estimate_is_the_calibration_reading() -> None:
    """Zero drift must reproduce the anchor exactly.

    If it does not, the intercept is wrong and every other estimate is offset by the same error.
    """
    result = _estimate()
    assert result is not None
    assert (result.systolic_mmhg, result.diastolic_mmhg) == (120, 80)
    assert result.confidence == 1.0


@pytest.mark.invariant
def test_shorter_ptt_raises_the_estimate_and_longer_lowers_it() -> None:
    """The sign of the relation, which is the one thing that must not be inverted.

    Higher pressure stiffens the artery, the wave travels faster, PTT shortens. An inverted sign
    would report a falling pressure exactly when it was rising — the most harmful single error
    this file could contain.
    """
    shorter = _estimate(ptt_now_ms=240.0)
    longer = _estimate(ptt_now_ms=260.0)
    assert shorter is not None and longer is not None
    assert shorter.systolic_mmhg > 120
    assert longer.systolic_mmhg < 120


@pytest.mark.invariant
def test_the_estimate_is_linear_in_drift_at_the_configured_sensitivity() -> None:
    settings = _settings()
    result = _estimate(ptt_now_ms=240.0, settings=settings)
    assert result is not None
    assert result.systolic_mmhg == round(120 + settings.systolic_mmhg_per_ms * 10)
    assert result.diastolic_mmhg == round(80 + settings.diastolic_mmhg_per_ms * 10)


@pytest.mark.invariant
def test_no_estimate_without_a_calibration_anchor() -> None:
    """Every missing input yields None, never a default.

    A plausible-looking number with nothing behind it is the failure mode this whole module is
    written to avoid.
    """
    assert _estimate(baseline_ptt_ms=None) is None
    assert _estimate(calibration_systolic=None) is None
    assert _estimate(calibration_diastolic=None) is None
    assert _estimate(calibration_established_at=None) is None
    assert _estimate(ptt_now_ms=None) is None


@pytest.mark.invariant
def test_no_estimate_once_the_calibration_has_aged_out() -> None:
    """The slope is population-derived and drifts with vascular tone.

    Past the window the anchor is not trustworthy, so a fresh cuff reading is asked for rather
    than an estimate extrapolated from a stale one.
    """
    settings = _settings()
    stale = NOW - timedelta(days=settings.max_calibration_age_days + 1)
    assert _estimate(calibration_established_at=stale, settings=settings) is None

    edge = NOW - timedelta(days=settings.max_calibration_age_days)
    assert _estimate(calibration_established_at=edge, settings=settings) is not None


@pytest.mark.invariant
def test_no_estimate_beyond_the_linear_range() -> None:
    """One calibration point fixes the intercept, not the slope.

    Far from the anchor the first-order approximation stops being defensible, so no number is
    produced — invariant 7, unchanged by the move to mmHg.
    """
    settings = _settings()
    beyond = 250.0 - (settings.max_ptt_drift_ms + 1)
    assert _estimate(ptt_now_ms=beyond, settings=settings) is None


@pytest.mark.invariant
def test_no_estimate_outside_the_physiological_clamps() -> None:
    """A result outside the clamps is a signal fault, not a finding, and is withheld."""
    settings = _settings()
    # A high anchor plus a large shortening would compute past the ceiling.
    assert (
        _estimate(
            calibration_systolic=settings.systolic_max_mmhg - 2,
            calibration_diastolic=130,
            ptt_now_ms=200.0,
            settings=settings,
        )
        is None
    )


@pytest.mark.invariant
def test_confidence_falls_away_from_the_calibration_point() -> None:
    """Confidence reports distance from the anchor, and is not a probability."""
    near = _estimate(ptt_now_ms=245.0)
    far = _estimate(ptt_now_ms=215.0)
    assert near is not None and far is not None
    assert near.confidence > far.confidence
    assert 0.0 <= far.confidence <= 1.0


@pytest.mark.invariant
def test_a_systolic_at_or_below_diastolic_is_refused() -> None:
    """Arithmetically reachable, physiologically not.

    Systolic and diastolic move at different sensitivities, so a narrow pulse pressure plus a
    long PTT can cross them over. The guard is what stops "88 / 91 mmHg" reaching a patient.
    """
    # Narrow starting gap, and a long PTT so both fall — systolic faster than diastolic.
    result = _estimate(
        calibration_systolic=95,
        calibration_diastolic=88,
        ptt_now_ms=270.0,
    )
    assert result is None
