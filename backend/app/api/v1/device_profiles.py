"""POST /v1/device-profiles — submit a profiling result, return an eligibility verdict."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import (
    DbDep,
    PrincipalDep,
    SettingsDep,
    assert_owns_or_404,
    assert_patient_scope,
)
from app.logging_config import get_logger
from app.models import AuditAction, DeviceProfile, Patient
from app.schemas.device import DeviceProfileCreate, DeviceProfileOut, EligibilityFindingOut
from app.services import audit
from app.services.eligibility import evaluate_device

router = APIRouter(prefix="/device-profiles", tags=["device-profiles"])
log = get_logger(__name__)


@router.post(
    "",
    response_model=DeviceProfileOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a profiling result and receive an eligibility verdict",
)
def submit_device_profile(
    body: DeviceProfileCreate,
    db: DbDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> DeviceProfileOut:
    """Grade a handset against the configured capability bands.

    The verdict is derived from the submitted measurements — nothing here estimates a value the
    profiler failed to measure (invariant 9).
    """
    assert_patient_scope(principal, body.patient_id)

    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="patient not found")

    verdict = evaluate_device(
        accel_rate_hz=body.accel_rate_hz,
        camera_fps=body.camera_fps,
        camera_hw_level=body.camera_hw_level,
        manual_sensor=body.manual_sensor,
        timestamp_source=body.timestamp_source,
        clock_offset_sd_ms=body.clock_offset_sd_ms,
        settings=settings.device,
    )

    profile = DeviceProfile(
        patient_id=body.patient_id,
        model=body.model,
        os_version=body.os_version,
        accel_rate_hz=body.accel_rate_hz,
        camera_fps=body.camera_fps,
        camera_hw_level=body.camera_hw_level,
        manual_sensor=body.manual_sensor,
        timestamp_source=body.timestamp_source,
        clock_offset_sd_ms=body.clock_offset_sd_ms,
        qualified_status=verdict.status,
        synthetic=body.synthetic,
    )
    db.add(profile)
    db.flush()

    audit.record(
        db,
        principal=principal,
        action=AuditAction.DEVICE_PROFILE_SUBMITTED,
        target=profile.id,
    )
    db.commit()

    # Structured log: ids, rates and gate outcome only. No clinical content (BUILD_SPEC 4.5).
    log.info(
        "device_profile_submitted",
        extra={
            "device_profile_id": str(profile.id),
            "accel_rate_hz": profile.accel_rate_hz,
            "camera_fps": profile.camera_fps,
            "qualified_status": profile.qualified_status.value,
        },
    )

    return _render(profile, verdict)


@router.get(
    "/{device_profile_id}",
    response_model=DeviceProfileOut,
    summary="Fetch a device profile and its verdict",
)
def get_device_profile(
    device_profile_id: uuid.UUID, db: DbDep, principal: PrincipalDep, settings: SettingsDep
) -> DeviceProfileOut:
    """Re-render a stored profile, recomputing the findings from the stored measurements."""
    profile = db.get(DeviceProfile, device_profile_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="device profile not found"
        )
    # 404, not 403 — see assert_owns_or_404. A device profile names a handset belonging to a
    # specific patient; confirming one exists is a disclosure about them.
    if not principal.is_clinician:
        assert_owns_or_404(principal, profile.patient_id, resource="device profile")

    verdict = evaluate_device(
        accel_rate_hz=profile.accel_rate_hz,
        camera_fps=profile.camera_fps,
        camera_hw_level=profile.camera_hw_level,
        manual_sensor=profile.manual_sensor,
        timestamp_source=profile.timestamp_source,
        clock_offset_sd_ms=profile.clock_offset_sd_ms,
        settings=settings.device,
    )
    return _render(profile, verdict)


def _render(profile: DeviceProfile, verdict) -> DeviceProfileOut:
    from app.schemas.common import SyntheticFlag

    return DeviceProfileOut(
        id=profile.id,
        patient_id=profile.patient_id,
        model=profile.model,
        os_version=profile.os_version,
        accel_rate_hz=profile.accel_rate_hz,
        camera_fps=profile.camera_fps,
        camera_hw_level=profile.camera_hw_level,
        manual_sensor=profile.manual_sensor,
        timestamp_source=profile.timestamp_source,
        clock_offset_sd_ms=profile.clock_offset_sd_ms,
        qualified_status=profile.qualified_status,
        submitted_at=profile.submitted_at,
        synthetic=profile.synthetic,
        synthetic_notice=SyntheticFlag.notice_for(profile.synthetic),
        findings=[
            EligibilityFindingOut(
                measurement=f.measurement,
                measured=f.measured,
                threshold=f.threshold,
                verdict=f.verdict,
                explanation=f.explanation,
            )
            for f in verdict.findings
        ],
    )
