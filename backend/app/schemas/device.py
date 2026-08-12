"""Device profile submission and eligibility verdict."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

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


class DeviceEligibilityIn(TeraModel):
    """`POST /v1/device/eligibility` — DEV-01's measurements.

    The same measured fields as :class:`DeviceProfileCreate` **minus `patient_id`**: this route is
    called by the patient's own handset, so the patient comes from the token. A body that could
    name a patient would let one handset write a hardware verdict onto somebody else's account.

    Every field is measured. Invariant 9: a probe that could not measure something must say so
    rather than substitute a plausible number.
    """

    model: str = Field(max_length=128)
    os_version: str = Field(max_length=64)
    accel_rate_hz: float = Field(gt=0, le=10_000, description="Measured, not requested, rate.")
    camera_fps: float = Field(gt=0, le=1_000, description="Sustained rate from frame timestamps.")
    camera_hw_level: CameraHardwareLevel
    manual_sensor: bool
    timestamp_source: TimestampSource
    clock_offset_sd_ms: float = Field(ge=0, le=10_000)


class DeviceEligibilityOut(SyntheticFlag, TeraModel):
    """DEV-02 / DEV-03's answer, in the app's two-way vocabulary.

    ``qualified_status`` is carried alongside rather than replaced: the profiler's three bands are
    the real verdict and the app's binary is a routing decision derived from it. A screen that
    wants to say "your phone meets the minimum but not the target" still can.
    """

    device_profile_id: uuid.UUID
    eligibility_status: Literal["eligible", "not_eligible"]
    qualified_status: QualifiedStatus
    model: str
    os_version: str
    accelerometer_supported: bool
    camera_supported: bool
    flash_supported: bool
    checked_at: datetime
    #: Which measurement decided it, when the answer was no. Null when nothing limited the verdict.
    detail: str | None = None
