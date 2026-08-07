"""Device profile submission and eligibility verdict."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.models.enums import CameraHardwareLevel, QualifiedStatus, TimestampSource
from app.schemas.common import SyntheticFlag, TeraModel


class DeviceProfileCreate(TeraModel):
    """A profiling result from the Phase 3 profiler.

    Every field is a *measured* value. Invariant 9: if the profiler could not measure something
    it must fail rather than substitute a plausible number, so there are no optional
    measurements here — an absent field is a rejected submission.
    """

    patient_id: uuid.UUID
    model: str = Field(max_length=128)
    os_version: str = Field(max_length=64)
    accel_rate_hz: float = Field(gt=0, le=10_000, description="Measured, not requested, rate.")
    camera_fps: float = Field(gt=0, le=1_000, description="Sustained rate from frame timestamps.")
    camera_hw_level: CameraHardwareLevel
    manual_sensor: bool
    timestamp_source: TimestampSource
    clock_offset_sd_ms: float = Field(
        ge=0,
        le=10_000,
        description="Spread of the realtime/uptime clock offset across repeated runs. "
        "Stability is what matters; a constant offset is absorbed by calibration.",
    )
    synthetic: bool = Field(
        default=False,
        description="Set true only for seeded demonstration profiles (invariant 9).",
    )


class EligibilityFindingOut(TeraModel):
    """One measurement's contribution to the verdict, with the numbers behind it."""

    measurement: str
    measured: str
    threshold: str
    verdict: QualifiedStatus
    explanation: str


class DeviceProfileOut(SyntheticFlag, TeraModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    model: str
    os_version: str
    accel_rate_hz: float
    camera_fps: float
    camera_hw_level: CameraHardwareLevel
    manual_sensor: bool
    timestamp_source: TimestampSource
    clock_offset_sd_ms: float
    qualified_status: QualifiedStatus
    submitted_at: datetime
    findings: list[EligibilityFindingOut] = Field(
        default_factory=list,
        description="Why the verdict came out as it did, measurement by measurement.",
    )
