"""Section 30's ``/device`` pair (PM spec section 6, DEV-01 to DEV-03).

Thin, on purpose. ``POST /v1/device-profiles`` already grades a handset against the configured
capability bands and stores the verdict; these two routes are the shape the app asks for, over the
same table and the same grader:

* ``POST /v1/device/eligibility`` — DEV-01's answer, in the app's vocabulary
  (``eligible`` / ``not_eligible``) rather than the profiler's three-way qualified band.
* ``GET /v1/device/current`` — the most recent verdict for this patient, so DEV-01 does not have to
  re-run a ten-second probe on a handset that has already been graded.

# Two vocabularies, deliberately kept apart

The profiler grades a handset ``qualified`` / ``provisional`` / ``not_qualified`` — three bands,
because a device between the minimum and the target genuinely is a third thing and the profiler's
job is to say so. The *app* branches two ways: sensor mode or BP-only. Collapsing happens here and
only here, and ``provisional`` collapses to eligible: the proposal's minimum band is the minimum at
which a capture is meaningful, and refusing it would put a working handset on the BP-only path.
The full three-way verdict is still returned alongside, so nothing is lost.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import desc, select

from app.api.deps import DbDep, PrincipalDep, SettingsDep, require_patient
from app.logging_config import get_logger
from app.models import AuditAction, DeviceProfile, QualifiedStatus
from app.schemas.device import DeviceEligibilityIn, DeviceEligibilityOut
from app.services import audit
from app.services.eligibility import evaluate_device

router = APIRouter(prefix="/device", tags=["device"])
log = get_logger(__name__)

#: Which profiler bands put a handset on the sensor path. ``provisional`` is included — see the
#: module docstring.
_ELIGIBLE_BANDS = frozenset({QualifiedStatus.QUALIFIED, QualifiedStatus.PROVISIONAL})


def _eligibility_out(profile: DeviceProfile, detail: str | None = None) -> DeviceEligibilityOut:
    eligible = profile.qualified_status in _ELIGIBLE_BANDS
    return DeviceEligibilityOut(
        device_profile_id=profile.id,
        eligibility_status="eligible" if eligible else "not_eligible",
        qualified_status=profile.qualified_status,
        model=profile.model,
        os_version=profile.os_version,
        accelerometer_supported=profile.accel_rate_hz > 0,
        camera_supported=profile.camera_fps > 0,
        # The profiler measures a torch-lit frame rate; a handset reporting frames at all has a
        # working camera path. There is no separate torch measurement to report, and inventing a
        # boolean nobody measured would be invariant 9 in miniature.
        flash_supported=profile.camera_fps > 0,
        checked_at=profile.submitted_at,
        detail=detail,
        synthetic=profile.synthetic,
    )


@router.post(
    "/eligibility",
    response_model=DeviceEligibilityOut,
    status_code=status.HTTP_201_CREATED,
    summary="DEV-01 — grade this handset and store the verdict",
)
def submit_eligibility(
    body: DeviceEligibilityIn,
    db: DbDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> DeviceEligibilityOut:
    """Grade the handset from what it measured, and record it.

    The patient comes from the token, never the body: a device profile is attached to a patient
    record, and letting a request name the patient would let one handset write a verdict onto
    somebody else's account.

    Nothing here estimates a figure the probe failed to produce (invariant 9). A handset that
    could not measure its accelerometer reports zero and is graded on that.
    """
    patient_id = require_patient(principal)

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
        patient_id=patient_id,
        model=body.model,
        os_version=body.os_version,
        accel_rate_hz=body.accel_rate_hz,
        camera_fps=body.camera_fps,
        camera_hw_level=body.camera_hw_level,
        manual_sensor=body.manual_sensor,
        timestamp_source=body.timestamp_source,
        clock_offset_sd_ms=body.clock_offset_sd_ms,
        qualified_status=verdict.status,
        synthetic=False,
    )
    db.add(profile)
    db.flush()

    audit.record(
        db, principal=principal, action=AuditAction.DEVICE_PROFILE_SUBMITTED, target=profile.id
    )
    db.commit()

    log.info(
        "device_eligibility_recorded",
        extra={
            "device_profile_id": str(profile.id),
            "accel_rate_hz": profile.accel_rate_hz,
            "camera_fps": profile.camera_fps,
            "qualified_status": profile.qualified_status.value,
        },
    )

    # The findings that actually decided the verdict, so DEV-03 can say which measurement was
    # the limiting one rather than "not suitable".
    return _eligibility_out(
        profile,
        detail="; ".join(
            f"{f.measurement}: {f.measured} (needs {f.threshold})"
            for f in verdict.limiting_findings
        )
        or None,
    )


@router.get(
    "/current",
    response_model=DeviceEligibilityOut,
    summary="The most recent verdict for this patient's handset",
)
def read_current(db: DbDep, principal: PrincipalDep) -> DeviceEligibilityOut:
    """The latest graded profile.

    404 when the handset has never been probed, which the app reads as "run DEV-01". It is not an
    error state: it is the answer on a fresh install, and it is why the check flow treats an
    unchecked device as not eligible rather than assuming.
    """
    patient_id = require_patient(principal)

    profile = (
        db.execute(
            select(DeviceProfile)
            .where(DeviceProfile.patient_id == patient_id)
            .order_by(desc(DeviceProfile.submitted_at))
            .limit(1)
        )
        .scalars()
        .first()
    )
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this handset has not been checked yet",
        )
    return _eligibility_out(profile)
