"""Estimated blood pressure from pulse transit time, anchored on a cuff calibration.

# What changed, and why this file exists

Invariant 1 used to read "no mmHg from SCG-PPG, ever". That was a product decision — the system
reported a *direction* and a magnitude in the patient's own baseline standard deviations, and only
`cuff_reading` ever held a pressure. The product owner has since decided Tera should do what
cuffless products such as Samsung Health Monitor do: calibrate once against a validated upper-arm
cuff, then estimate mmHg from PTT on later captures. The invariant has been rewritten in both
`CLAUDE.md` files to match, and the reasoning is in `docs/decisions.md`.

The line that has *not* moved: an estimate is computed from a real measurement through a stated
model, and is never presented in the same visual language as a cuff reading. A number that no
signal produced is still forbidden — see `estimate()` returning `None` rather than a plausible
guess in every case it cannot stand behind.

# The model

One calibration point fixes the **intercept**. It cannot fix the **slope**, which comes from
population data and is not personalised:

    SBP_est = SBP_cal + k_sys * (PTT_cal - PTT_now)
    DBP_est = DBP_cal + k_dia * (PTT_cal - PTT_now)

with PTT in milliseconds. Shorter transit means a stiffer, more pressurised artery, hence the
sign. This is the first-order approximation of Moens-Korteweg with Hughes' exponential
elasticity, valid over the narrow range a resting adult spans and increasingly wrong outside it.

# When no number is produced

Deliberately often, because the alternative is a confident wrong answer:

  * no calibration in force  — nothing to anchor to
  * calibration older than `max_calibration_age_days` — the slope drifts with vascular tone
  * PTT drift beyond `max_ptt_drift_ms` — outside the linear range
  * a result outside the clamps — a signal fault, not a physiological finding
  * no usable PTT for this session

Every one of those returns `None`, and the caller shows the direction-only result it showed
before. Invariant 7 is unchanged: where the signal does not support a number, there is no number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import PressureEstimateSettings


@dataclass(frozen=True)
class PressureEstimate:
    """An estimate, with the distance from its anchor attached.

    ``confidence`` is not a probability and is not presented as one. It is the fraction of the
    permitted drift window still unused — 1.0 at the calibration point, 0.0 at the edge — so a
    client can show how far the reading has travelled from the thing that makes it meaningful.
    """

    systolic_mmhg: int
    diastolic_mmhg: int
    ptt_drift_ms: float
    confidence: float
    calibration_age_days: int


def estimate(
    *,
    ptt_now_ms: float | None,
    baseline_ptt_ms: float | None,
    calibration_systolic: int | None,
    calibration_diastolic: int | None,
    calibration_established_at: datetime | None,
    settings: PressureEstimateSettings,
    now: datetime | None = None,
) -> PressureEstimate | None:
    """Return an estimate, or `None` when one cannot honestly be produced.

    Never raises and never substitutes a default: every rejection path returns `None` so the
    caller falls back to the direction-only result rather than to an invented pressure.
    """
    if (
        ptt_now_ms is None
        or baseline_ptt_ms is None
        or calibration_systolic is None
        or calibration_diastolic is None
        or calibration_established_at is None
    ):
        return None
    if ptt_now_ms <= 0 or baseline_ptt_ms <= 0:
        return None

    moment = now or datetime.now(tz=timezone.utc)
    anchored = calibration_established_at
    if anchored.tzinfo is None:
        anchored = anchored.replace(tzinfo=timezone.utc)
    age_days = (moment - anchored).days
    if age_days > settings.max_calibration_age_days:
        # The anchor has aged out. A cuff reading is asked for instead — the same escalation the
        # system already performs when calibration state is ambiguous.
        return None

    drift = baseline_ptt_ms - ptt_now_ms
    if abs(drift) > settings.max_ptt_drift_ms:
        return None

    systolic = round(calibration_systolic + settings.systolic_mmhg_per_ms * drift)
    diastolic = round(calibration_diastolic + settings.diastolic_mmhg_per_ms * drift)

    if not (settings.systolic_min_mmhg <= systolic <= settings.systolic_max_mmhg):
        return None
    if not (settings.diastolic_min_mmhg <= diastolic <= settings.diastolic_max_mmhg):
        return None
    # A systolic at or below diastolic is arithmetically possible here and physiologically not.
    if systolic <= diastolic:
        return None

    confidence = 1.0 - (abs(drift) / settings.max_ptt_drift_ms)
    return PressureEstimate(
        systolic_mmhg=systolic,
        diastolic_mmhg=diastolic,
        ptt_drift_ms=drift,
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        calibration_age_days=age_days,
    )
