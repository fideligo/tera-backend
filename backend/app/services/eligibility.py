"""Device eligibility verdict for POST /v1/device-profiles.

Answers one question: can this handset run Tera? The verdict is derived from *measured* numbers
submitted by the Phase 3 profiler. Nothing here estimates a value the profiler failed to measure
(invariant 9) — a missing measurement is a failed profile, not an assumed one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import DeviceEligibilitySettings
from app.models.enums import CameraHardwareLevel, QualifiedStatus, TimestampSource

#: Verdicts ordered from best to worst, so a demotion is a max() over ordinals.
_SEVERITY = {
    QualifiedStatus.QUALIFIED: 0,
    QualifiedStatus.PROVISIONAL: 1,
    QualifiedStatus.NOT_QUALIFIED: 2,
}


@dataclass(frozen=True)
class EligibilityFinding:
    """One measurement's contribution to the verdict.

    Rendered in the Phase 2 device-profile screen: the measured number, the threshold it was
    compared against, and what it means. A finding is never a bare pass/fail.
    """

    measurement: str
    measured: str
    threshold: str
    verdict: QualifiedStatus
    explanation: str


@dataclass(frozen=True)
class EligibilityVerdict:
    status: QualifiedStatus
    findings: list[EligibilityFinding]

    @property
    def limiting_findings(self) -> list[EligibilityFinding]:
        """The findings that actually decided the verdict."""
        return [f for f in self.findings if f.verdict is self.status]


def evaluate_device(
    *,
    accel_rate_hz: float,
    camera_fps: float,
    camera_hw_level: CameraHardwareLevel,
    manual_sensor: bool,
    timestamp_source: TimestampSource,
    clock_offset_sd_ms: float,
    settings: DeviceEligibilitySettings,
) -> EligibilityVerdict:
    """Grade a handset against the configured bands.

    The overall verdict is the worst individual finding: a device is only as capable as its
    weakest relevant property, and a phone that samples at 400 Hz but cannot lock exposure
    cannot produce a usable PPG regardless of the accelerometer.
    """
    findings = [
        _grade_accelerometer(accel_rate_hz, settings),
        _grade_camera_rate(camera_fps, settings),
        _grade_hardware_level(camera_hw_level, settings),
        _grade_manual_sensor(manual_sensor),
        _grade_timestamp_source(timestamp_source, settings),
        _grade_clock_stability(clock_offset_sd_ms, settings),
    ]
    status = max((f.verdict for f in findings), key=lambda v: _SEVERITY[v])
    return EligibilityVerdict(status=status, findings=findings)


def _grade_accelerometer(
    accel_rate_hz: float, settings: DeviceEligibilitySettings
) -> EligibilityFinding:
    if accel_rate_hz >= settings.accel_rate_qualified_hz:
        verdict, explanation = (
            QualifiedStatus.QUALIFIED,
            "Sample interval is fine enough to place the aortic-valve-opening landmark to "
            "within a few milliseconds.",
        )
    elif accel_rate_hz >= settings.accel_rate_provisional_hz:
        verdict, explanation = (
            QualifiedStatus.PROVISIONAL,
            "Usable, but the sample interval is coarse relative to the transit-time changes "
            "being tracked, so more of the measurement is interpolation.",
        )
    else:
        verdict, explanation = (
            QualifiedStatus.NOT_QUALIFIED,
            "Sample interval is too coarse to locate the landmark the measurement depends on.",
        )
    return EligibilityFinding(
        measurement="Accelerometer achieved rate",
        measured=f"{accel_rate_hz:.1f} Hz",
        threshold=f">= {settings.accel_rate_qualified_hz:.0f} Hz qualified, "
        f">= {settings.accel_rate_provisional_hz:.0f} Hz provisional",
        verdict=verdict,
        explanation=explanation,
    )


def _grade_camera_rate(
    camera_fps: float, settings: DeviceEligibilitySettings
) -> EligibilityFinding:
    if camera_fps >= settings.camera_fps_qualified:
        verdict, explanation = (
            QualifiedStatus.QUALIFIED,
            "Frame interval is short enough that pulse arrival is well localised between "
            "frames.",
        )
    elif camera_fps >= settings.camera_fps_provisional:
        verdict, explanation = (
            QualifiedStatus.PROVISIONAL,
            "Workable, but each frame spans a large fraction of the interval being measured, so "
            "arrival time leans on interpolation.",
        )
    else:
        verdict, explanation = (
            QualifiedStatus.NOT_QUALIFIED,
            "Too few frames per pulse to locate arrival time.",
        )
    return EligibilityFinding(
        measurement="Camera sustained frame rate",
        measured=f"{camera_fps:.1f} fps",
        threshold=f">= {settings.camera_fps_qualified:.0f} fps qualified, "
        f">= {settings.camera_fps_provisional:.0f} fps provisional",
        verdict=verdict,
        explanation=explanation,
    )


def _grade_hardware_level(
    level: CameraHardwareLevel, settings: DeviceEligibilitySettings
) -> EligibilityFinding:
    if level.value in settings.acceptable_hardware_levels_qualified:
        verdict, explanation = (
            QualifiedStatus.QUALIFIED,
            "Exposure, white balance and focus can be locked, so frame-to-frame brightness "
            "changes come from the pulse rather than from the camera adjusting itself.",
        )
    elif level.value in settings.acceptable_hardware_levels_provisional:
        verdict, explanation = (
            QualifiedStatus.PROVISIONAL,
            "Some capture controls are available but not all; residual auto-adjustment may "
            "contaminate the intensity series.",
        )
    else:
        verdict, explanation = (
            QualifiedStatus.NOT_QUALIFIED,
            "Capture controls cannot be locked. Auto-exposure would change the very brightness "
            "signal the measurement reads.",
        )
    return EligibilityFinding(
        measurement="Camera hardware level",
        measured=level.value,
        threshold="full or level_3 qualified, limited provisional",
        verdict=verdict,
        explanation=explanation,
    )


def _grade_manual_sensor(manual_sensor: bool) -> EligibilityFinding:
    return EligibilityFinding(
        measurement="MANUAL_SENSOR capability",
        measured="present" if manual_sensor else "absent",
        threshold="present for qualified",
        verdict=QualifiedStatus.QUALIFIED if manual_sensor else QualifiedStatus.PROVISIONAL,
        explanation=(
            "Manual sensor control allows exposure and gain to be pinned for the whole capture."
            if manual_sensor
            else "Without manual sensor control, exposure and gain can drift mid-capture and "
            "add brightness changes that did not come from the pulse."
        ),
    )


def _grade_timestamp_source(
    source: TimestampSource, settings: DeviceEligibilitySettings
) -> EligibilityFinding:
    realtime = source is TimestampSource.REALTIME
    if realtime or not settings.require_realtime_timestamp_source_for_qualified:
        verdict = QualifiedStatus.QUALIFIED
    else:
        verdict = QualifiedStatus.PROVISIONAL
    return EligibilityFinding(
        measurement="Camera timestamp source",
        measured=source.value,
        threshold="realtime for qualified",
        verdict=verdict,
        explanation=(
            "Camera and accelerometer timestamps share a time base, so the interval between "
            "them is read directly."
            if realtime
            else "Camera and accelerometer use different time bases, so the two signals must be "
            "aligned through an inferred offset — the error this measurement is most sensitive "
            "to."
        ),
    )


def _grade_clock_stability(
    clock_offset_sd_ms: float, settings: DeviceEligibilitySettings
) -> EligibilityFinding:
    if clock_offset_sd_ms <= settings.clock_offset_sd_qualified_ms:
        verdict, explanation = (
            QualifiedStatus.QUALIFIED,
            "Offset between the two clocks is stable. A constant offset is absorbed by personal "
            "calibration; it is the variation that matters.",
        )
    elif clock_offset_sd_ms <= settings.clock_offset_sd_provisional_ms:
        verdict, explanation = (
            QualifiedStatus.PROVISIONAL,
            "Offset varies enough to add noise of the same order as the changes being tracked.",
        )
    else:
        verdict, explanation = (
            QualifiedStatus.NOT_QUALIFIED,
            "Offset varies by more than the effect being measured, so an apparent change could "
            "be entirely clock drift.",
        )
    return EligibilityFinding(
        measurement="Clock offset stability",
        measured=f"{clock_offset_sd_ms:.2f} ms SD",
        threshold=f"<= {settings.clock_offset_sd_qualified_ms:.1f} ms qualified, "
        f"<= {settings.clock_offset_sd_provisional_ms:.1f} ms provisional",
        verdict=verdict,
        explanation=explanation,
    )
