"""The deviation engine (BUILD_SPEC 4.3).

Pure functions, no database access, so every rule here is unit-testable in isolation.

**Invariant 1 governs this whole module.** Nothing here computes, returns or approximates a
blood-pressure value. The output is a direction and a count of the patient's own baseline
standard deviations. There is no conversion from ``magnitude_sd`` to mmHg, deliberately, and one
must not be added: the relationship between PTT and pressure is individual, posture-dependent
and drifts with vascular state, which is exactly why the cuff remains the reference.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from app.config import DeviationSettings
from app.models.enums import DeviationState, TrendDirection


@dataclass(frozen=True)
class SessionPtt:
    """The single number that represents one session."""

    #: Trimmed mean of the usable beats, in milliseconds.
    value_ms: float
    #: How many beats survived the IQR trim.
    n_retained: int
    #: How many beats went into the trim.
    n_considered: int


@dataclass(frozen=True)
class Baseline:
    """A patient's personal reference, in their own units."""

    mean_ms: float
    sd_ms: float
    n_sessions: int

    #: True when [sd_ms] is the clinical floor standing in for a spread that could not be measured.
    #:
    #: A single calibration session has no between-session variation to estimate from. The figure
    #: in [sd_ms] is then a policy threshold wearing the units of a spread, not an observation of
    #: this patient, and anything reporting "standard deviations of your own baseline" to a reader
    #: needs to be able to tell the difference.
    sd_is_provisional: bool = False


@dataclass(frozen=True)
class DeviationResult:
    """The engine's verdict for one session.

    ``magnitude_sd`` is a count of baseline standard deviations. It is not a pressure and does
    not convert to one.
    """

    direction: TrendDirection
    magnitude_sd: float
    confidence: float
    deviation_state: DeviationState

    @property
    def requests_cuff(self) -> bool:
        """BUILD_SPEC 4.3 — only a *persistent* deviation asks for a cuff reading.

        A single deviating session is expected noise: posture, recent activity, a cold room and
        the time of day all move PTT without pressure moving with it.
        """
        return self.deviation_state is DeviationState.PERSISTENT


def trimmed_session_ptt(ptt_ms: list[float], settings: DeviationSettings) -> SessionPtt:
    """Session-level PTT: the trimmed mean of usable beats.

    BUILD_SPEC 4.3: "discard beyond 1.5 x IQR". That is the standard Tukey fence — it removes
    the beats where the fiducial point was misplaced without letting a single bad beat move the
    session value.

    With fewer than four beats a quartile is not defined, so the plain mean is returned; such a
    session is far below ``min_usable_beats`` anyway and will not produce an estimate.
    """
    if not ptt_ms:
        raise ValueError("cannot compute a session PTT from zero beats")

    values = sorted(float(v) for v in ptt_ms)
    if len(values) < 4:
        return SessionPtt(
            value_ms=statistics.fmean(values), n_retained=len(values), n_considered=len(values)
        )

    q1, _median, q3 = statistics.quantiles(values, n=4, method="inclusive")
    fence = settings.iqr_fence_multiplier * (q3 - q1)
    lower, upper = q1 - fence, q3 + fence
    retained = [v for v in values if lower <= v <= upper]

    # A degenerate IQR (every beat identical) can leave the fence at zero width; the values are
    # then all equal anyway, so falling back to the full set changes nothing but avoids an
    # empty list.
    if not retained:
        retained = values

    return SessionPtt(
        value_ms=statistics.fmean(retained),
        n_retained=len(retained),
        n_considered=len(values),
    )


def compute_baseline(session_ptt_values: list[float], settings: DeviationSettings) -> Baseline:
    """Baseline = mean and SD of session-level PTT across the calibration sessions.

    BUILD_SPEC 4.3 requires "at least three accepted calibration sessions". The sample standard
    deviation (n-1) is used because these sessions are a sample of the patient's day-to-day
    variation, not the whole of it.
    """
    if len(session_ptt_values) < settings.min_calibration_sessions:
        raise ValueError(
            f"a baseline needs at least {settings.min_calibration_sessions} calibration "
            f"sessions, got {len(session_ptt_values)}"
        )

    # **One session: a provisional spread, not a refusal.**
    #
    # A single value has no sample standard deviation — `statistics.stdev` raises on it — and this
    # used to refuse for that reason. Refusing was the wrong answer to the right observation. The
    # arithmetic really is undefined, but `min_calibration_sessions` is 1 *because single-point
    # calibration is the product*: the patient is told "take one cuff reading", and the estimate
    # path was already built for it — `pressure_estimate` fixes the intercept from one anchor and
    # takes the slope from population coefficients (invariant 1). Only the deviation engine needed
    # a spread, and it was blocking a mmHg estimate that never depended on one.
    #
    # The ML reference states the fallback directly, in `classify_trend`: "a fixed floor at the
    # bottom of that band until enough baseline sessions exist to estimate the between-session SD
    # properly, then 2 sigma of THAT." Its threshold is `max(min_delta_ms, 2.0 * sd)`.
    #
    # So the floor is expressed here as the sigma that reproduces it. At the default `deviation_k`
    # of 2, `k * (trend_min_delta_ms / 2)` is exactly `trend_min_delta_ms`, which is the
    # reference's floor to the millisecond; an episode that sets a stricter k gets a
    # proportionally stricter floor, which is the behaviour that constant is for.
    #
    # It is flagged, because it is a threshold wearing the units of an observation.
    if len(session_ptt_values) == 1:
        return Baseline(
            mean_ms=float(session_ptt_values[0]),
            sd_ms=settings.trend_min_delta_ms / 2.0,
            n_sessions=1,
            sd_is_provisional=True,
        )

    mean_ms = statistics.fmean(session_ptt_values)
    sd_ms = statistics.stdev(session_ptt_values)

    if sd_ms <= 0:
        # Identical session values across three or more captures means the device is not
        # resolving real variation. Escalating (invariant 7) beats recording a baseline whose
        # zero spread would make every later session look like an extreme deviation.
        raise ValueError(
            "baseline standard deviation is zero — the calibration sessions show no "
            "measurable variation, so no usable baseline can be derived from them"
        )

    return Baseline(mean_ms=mean_ms, sd_ms=sd_ms, n_sessions=len(session_ptt_values))


def classify_direction(
    session_ptt_ms: float, baseline: Baseline, deviation_k: float
) -> tuple[TrendDirection, float]:
    """Return the direction and the magnitude in baseline standard deviations.

    Physiology, because the sign is easy to get backwards: **shorter PTT means the pulse wave
    travelled faster.** A faster wave implies a stiffer arterial wall, and arterial stiffness
    rises with distending pressure. So a *shorter* transit time is associated with *higher*
    blood pressure, and maps to ``increase``.

    That is an association in the patient's own baseline units. It is not a measurement of
    pressure and it does not convert to one (invariant 1).
    """
    delta_ms = session_ptt_ms - baseline.mean_ms
    magnitude_sd = abs(delta_ms) / baseline.sd_ms

    if magnitude_sd < deviation_k:
        return TrendDirection.STABLE, magnitude_sd

    return (
        TrendDirection.INCREASE if delta_ms < 0 else TrendDirection.DECREASE,
        magnitude_sd,
    )


def compute_confidence(
    *,
    n_usable_beats: int,
    quality: dict,
    min_usable_beats: int,
    settings: DeviationSettings,
) -> float:
    """A deliberately simple, deliberately blunt heuristic.

    BUILD_SPEC 4.3: "Do not invent a formula that implies clinical accuracy — keep it simple,
    documented, and clearly a heuristic." So:

    * **Beat term** rises linearly with usable beats and saturates at a configured absolute
      count. More beats past that point do not make the estimate better. It is deliberately not
      a multiple of ``min_usable_beats``: where the gate is set and where extra signal stops
      helping are unrelated, and tying them meant lowering the gate quietly raised the score.
    * **Quality term** is the *worst* of the SNR, motion and dropped-frame sub-scores, not their
      average. Averaging would let a good SNR hide a capture ruined by motion; taking the worst
      limb is the escalation-biased choice (invariant 7).
    * The result is scaled into ``[floor, ceiling]`` with the ceiling strictly below 1.0, so no
      response can be read as certainty.

    This number orders sessions by how much the signal supported the reading. It is not a
    probability, not a confidence interval, and carries no clinical accuracy claim.
    """
    # The larger of the configured saturation count and the floor in force. A deployment that
    # demanded more beats than the saturation point would otherwise saturate every session it
    # accepted, which would make the beat term constant and therefore useless.
    saturation = max(
        1.0, float(settings.confidence_beat_saturation_beats), float(min_usable_beats)
    )
    beat_score = _clamp(n_usable_beats / saturation, 0.0, 1.0)

    snr_span = settings.confidence_snr_db_ceiling - settings.confidence_snr_db_floor
    snr_score = _clamp(
        (float(quality.get("snr_db", 0.0)) - settings.confidence_snr_db_floor) / snr_span, 0.0, 1.0
    )
    motion_score = _clamp(1.0 - float(quality.get("motion_index", 1.0)), 0.0, 1.0)
    frame_score = _clamp(1.0 - float(quality.get("dropped_frame_pct", 100.0)) / 100.0, 0.0, 1.0)
    quality_score = min(snr_score, motion_score, frame_score)

    weighted = (
        settings.confidence_beat_weight * beat_score
        + settings.confidence_quality_weight * quality_score
    )
    span = settings.confidence_ceiling - settings.confidence_floor
    return round(settings.confidence_floor + span * _clamp(weighted, 0.0, 1.0), 4)


def resolve_deviation_state(
    *,
    direction: TrendDirection,
    prior_deviating_direction: TrendDirection | None,
) -> DeviationState:
    """Decide whether this session's deviation is possible or persistent.

    BUILD_SPEC 4.3: "``persistent`` when a repeat session within the configured window also
    deviates. A single deviating session never triggers a cuff request."

    ``prior_deviating_direction`` is the direction of the most recent estimate inside the
    persistence window, or None if there was none or it was stable. Persistence requires the
    same direction: a session that reads high followed by one that reads low is instability, not
    a trend, and asking for a cuff on that pairing would train the patient to ignore the request.
    """
    if direction is TrendDirection.STABLE:
        return DeviationState.NONE
    if prior_deviating_direction == direction:
        return DeviationState.PERSISTENT
    return DeviationState.POSSIBLE


def evaluate(
    *,
    ptt_ms: list[float],
    baseline: Baseline,
    quality: dict,
    n_usable_beats: int,
    deviation_k: float,
    min_usable_beats: int,
    prior_deviating_direction: TrendDirection | None,
    settings: DeviationSettings,
) -> tuple[SessionPtt, DeviationResult]:
    """Run the whole engine for one session."""
    session_ptt = trimmed_session_ptt(ptt_ms, settings)
    direction, magnitude_sd = classify_direction(session_ptt.value_ms, baseline, deviation_k)
    confidence = compute_confidence(
        n_usable_beats=n_usable_beats,
        quality=quality,
        min_usable_beats=min_usable_beats,
        settings=settings,
    )
    state = resolve_deviation_state(
        direction=direction, prior_deviating_direction=prior_deviating_direction
    )
    return session_ptt, DeviationResult(
        direction=direction,
        magnitude_sd=round(magnitude_sd, 4),
        confidence=confidence,
        deviation_state=state,
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
