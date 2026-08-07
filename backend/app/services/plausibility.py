"""Server-side payload plausibility (BUILD_SPEC 4.4, defence in depth).

The quality gate runs on the device. This module exists because the backend must not trust the
client: a handset with a bug, a stale build, or a replay tool pointed at the wrong file can all
produce a payload that looks plausible to the handset and is not.

Every violation here becomes a 422. Nothing here is a clinical judgement — a payload that fails
these checks is malformed, not abnormal.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import PlausibilitySettings


@dataclass(frozen=True)
class Violation:
    """One reason a payload was rejected. ``field`` is a JSON pointer-ish path for the client."""

    field: str
    message: str


def check_session_payload(
    *,
    ptt_ms: list[float],
    n_beats_total: int,
    n_beats_usable: int,
    quality: dict,
    status_is_completed: bool,
    min_usable_beats: int,
    profile_accel_rate_hz: float,
    profile_camera_fps: float,
    settings: PlausibilitySettings,
) -> list[Violation]:
    """Return every plausibility violation in a session payload.

    All checks run rather than short-circuiting, so a device developer gets the full list back
    in one response instead of fixing them one round trip at a time.
    """
    violations: list[Violation] = []

    # --- invariant 2: the array must not be usable as a waveform channel --------------------
    if len(ptt_ms) > settings.max_ptt_array_length:
        violations.append(
            Violation(
                "ptt_ms",
                f"contains {len(ptt_ms)} intervals, above the maximum of "
                f"{settings.max_ptt_array_length}. The API accepts one derived interval per "
                f"beat and nothing deeper.",
            )
        )

    out_of_range = [
        value
        for value in ptt_ms
        if not (settings.ptt_min_ms <= value <= settings.ptt_max_ms)
    ]
    if out_of_range:
        # The offending values are counted, not echoed: a 422 body is not a place for
        # per-beat physiological data.
        violations.append(
            Violation(
                "ptt_ms",
                f"{len(out_of_range)} interval(s) fall outside the plausible range "
                f"{settings.ptt_min_ms:.0f}-{settings.ptt_max_ms:.0f} ms.",
            )
        )

    # --- beat accounting --------------------------------------------------------------------
    if n_beats_usable > n_beats_total:
        violations.append(
            Violation(
                "n_beats_usable",
                f"is {n_beats_usable}, above n_beats_total of {n_beats_total}. More beats "
                f"cannot survive the gate than were detected.",
            )
        )

    # One interval per usable beat. A mismatch means the array and the counts describe different
    # things, and there is no safe way to guess which one is right.
    if len(ptt_ms) != n_beats_usable:
        violations.append(
            Violation(
                "ptt_ms",
                f"contains {len(ptt_ms)} intervals but n_beats_usable is {n_beats_usable}. "
                f"The array carries exactly one interval per usable beat.",
            )
        )

    if status_is_completed and n_beats_usable < min_usable_beats:
        violations.append(
            Violation(
                "n_beats_usable",
                f"is {n_beats_usable}, below the minimum of {min_usable_beats} for a completed "
                f"session. A session with too few usable beats is rejected, not completed.",
            )
        )

    # --- quality block ----------------------------------------------------------------------
    missing = [field for field in settings.required_quality_fields if field not in quality]
    if missing:
        violations.append(
            Violation("quality", f"missing required field(s): {', '.join(sorted(missing))}.")
        )

    violations.extend(_check_quality_ranges(quality, settings))

    # --- achieved rates against the device's qualified band ---------------------------------
    #
    # CONFLICT RESOLVED IN FAVOUR OF INVARIANT 3, flagged in docs/decisions.md.
    #
    # BUILD_SPEC 4.4 lists "achieved rates below the device profile's qualified band" as a 422
    # without qualifying it by status. Applied to a *rejected* session that would be wrong: a
    # session rejected for `sensor_rate_below_qualified` reports low rates precisely because
    # that is why it failed, and 422 would discard it. Invariant 3 says rejected sessions are
    # retained, never discarded, and section 2 says an invariant wins a conflict.
    #
    # So the rate check gates *completed* sessions only. The structural checks above still
    # apply to every payload, because invariant 2 does not bend for a rejected session.
    if not status_is_completed:
        return violations

    tolerance = 1.0 - settings.achieved_rate_tolerance_fraction
    achieved_accel = _as_float(quality.get("accel_rate_hz"))
    if achieved_accel is not None and achieved_accel < profile_accel_rate_hz * tolerance:
        violations.append(
            Violation(
                "quality.accel_rate_hz",
                f"achieved {achieved_accel:.1f} Hz, below the band this device profile "
                f"qualified in ({profile_accel_rate_hz:.1f} Hz less "
                f"{settings.achieved_rate_tolerance_fraction:.0%} tolerance).",
            )
        )

    achieved_fps = _as_float(quality.get("camera_fps"))
    if achieved_fps is not None and achieved_fps < profile_camera_fps * tolerance:
        violations.append(
            Violation(
                "quality.camera_fps",
                f"achieved {achieved_fps:.1f} fps, below the band this device profile "
                f"qualified in ({profile_camera_fps:.1f} fps less "
                f"{settings.achieved_rate_tolerance_fraction:.0%} tolerance).",
            )
        )

    return violations


def _check_quality_ranges(quality: dict, settings: PlausibilitySettings) -> list[Violation]:
    """Bound the quality metrics themselves, so a nonsense value cannot pass the gate."""
    bounds = {
        "snr_db": (settings.snr_db_min, settings.snr_db_max),
        "motion_index": (settings.motion_index_min, settings.motion_index_max),
        "dropped_frame_pct": (settings.dropped_frame_pct_min, settings.dropped_frame_pct_max),
    }

    violations: list[Violation] = []
    for field, (low, high) in bounds.items():
        if field not in quality:
            continue  # absence is reported by the missing-fields check
        value = _as_float(quality[field])
        if value is None:
            violations.append(Violation(f"quality.{field}", "is not a number."))
        elif not (low <= value <= high):
            violations.append(
                Violation(f"quality.{field}", f"is {value}, outside the range {low} to {high}.")
            )

    for field in ("accel_rate_hz", "camera_fps"):
        if field in quality:
            value = _as_float(quality[field])
            if value is None or value <= 0:
                violations.append(
                    Violation(f"quality.{field}", "must be a positive number.")
                )

    return violations


def check_cuff_reading(
    *, systolic_mmhg: int, diastolic_mmhg: int, pulse_bpm: int | None,
    settings: PlausibilitySettings,
) -> list[Violation]:
    """Plausibility ranges for a cuff reading (BUILD_SPEC 4.1).

    These are data-entry filters, not clinical thresholds. A value inside the range is not
    "normal" and a value at the edge is not an alarm — the system does not make that judgement
    (invariant 6).
    """
    violations: list[Violation] = []

    if not (settings.systolic_min_mmhg <= systolic_mmhg <= settings.systolic_max_mmhg):
        violations.append(
            Violation(
                "systolic_mmhg",
                f"must be between {settings.systolic_min_mmhg} and {settings.systolic_max_mmhg}.",
            )
        )
    if not (settings.diastolic_min_mmhg <= diastolic_mmhg <= settings.diastolic_max_mmhg):
        violations.append(
            Violation(
                "diastolic_mmhg",
                f"must be between {settings.diastolic_min_mmhg} and "
                f"{settings.diastolic_max_mmhg}.",
            )
        )
    if systolic_mmhg <= diastolic_mmhg:
        violations.append(
            Violation("systolic_mmhg", "must be above diastolic_mmhg.")
        )
    if pulse_bpm is not None and not (
        settings.pulse_min_bpm <= pulse_bpm <= settings.pulse_max_bpm
    ):
        violations.append(
            Violation(
                "pulse_bpm",
                f"must be between {settings.pulse_min_bpm} and {settings.pulse_max_bpm}.",
            )
        )

    return violations


def _as_float(value: object) -> float | None:
    """Coerce to float, returning None for anything that is not a number."""
    if isinstance(value, bool):  # bool is an int subclass; a boolean rate is not a number
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None
