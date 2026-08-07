"""Calibration lifecycle (invariant 4).

Two things happen here and nothing else may:

* **Resolution** — which calibration was in force at a given instant. Not "which is active now".
* **Establishment** — inserting a new calibration and, if one was already active for the same
  patient and device, marking it superseded. The old row's baseline is never touched; a database
  trigger enforces that independently of this module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Calibration,
    CalibrationSourceSession,
    CalibrationStatus,
    CuffReading,
    DeviceProfile,
    MeasurementSession,
    SessionStatus,
)
from app.services.deviation import Baseline, compute_baseline, trimmed_session_ptt


class CalibrationError(Exception):
    """A calibration request that cannot be satisfied. Maps to HTTP 422."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True)
class EstablishedCalibration:
    calibration: Calibration
    superseded: Calibration | None
    source_session_ptts: dict[uuid.UUID, float]


def resolve_at(
    session: Session,
    *,
    patient_id: uuid.UUID,
    device_profile_id: uuid.UUID,
    at: datetime,
) -> Calibration | None:
    """Return the calibration in force for this patient and device at ``at``.

    Invariant 4: "Every estimate references the calibration in force **at capture time**." Not
    the currently active one — if a patient recalibrates on Tuesday and a Monday session uploads
    on Wednesday, that session belongs to the Monday baseline. Interpreting it against Tuesday's
    would compare a measurement to a reference that did not exist when it was taken.
    """
    stmt = (
        select(Calibration)
        .where(
            Calibration.patient_id == patient_id,
            Calibration.device_profile_id == device_profile_id,
            Calibration.established_at <= at,
            (Calibration.superseded_at.is_(None)) | (Calibration.superseded_at > at),
        )
        .order_by(Calibration.established_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def active_for_device(
    session: Session, *, patient_id: uuid.UUID, device_profile_id: uuid.UUID
) -> Calibration | None:
    """The currently active calibration, if any. At most one by the partial unique index."""
    stmt = select(Calibration).where(
        Calibration.patient_id == patient_id,
        Calibration.device_profile_id == device_profile_id,
        Calibration.status == CalibrationStatus.ACTIVE,
    )
    return session.execute(stmt).scalar_one_or_none()


def establish(
    session: Session,
    *,
    patient_id: uuid.UUID,
    device_profile_id: uuid.UUID,
    reference_cuff_reading_id: uuid.UUID,
    session_ids: list[uuid.UUID],
    settings: Settings,
    synthetic: bool = False,
    now: datetime | None = None,
) -> EstablishedCalibration:
    """Establish a calibration, superseding any active one for the same patient and device.

    The baseline is computed here from the named sessions. It is never accepted from the client:
    a handset that could write its own baseline could make any later session look stable.
    """
    now = now or datetime.now(tz=timezone.utc)

    device_profile = session.get(DeviceProfile, device_profile_id)
    if device_profile is None:
        raise CalibrationError("device_profile_id", "device profile does not exist")
    if device_profile.patient_id != patient_id:
        raise CalibrationError(
            "device_profile_id", "device profile belongs to a different patient"
        )

    cuff_reading = session.get(CuffReading, reference_cuff_reading_id)
    if cuff_reading is None:
        raise CalibrationError(
            "reference_cuff_reading_id", "reference cuff reading does not exist"
        )

    source_ptts = _collect_session_ptts(
        session,
        session_ids=session_ids,
        patient_id=patient_id,
        device_profile_id=device_profile_id,
        settings=settings,
    )

    try:
        baseline: Baseline = compute_baseline(list(source_ptts.values()), settings.deviation)
    except ValueError as exc:
        # Invariant 7 — an unusable baseline means no calibration, not a guessed one.
        raise CalibrationError("session_ids", str(exc)) from exc

    superseded = active_for_device(
        session, patient_id=patient_id, device_profile_id=device_profile_id
    )

    # The new id is generated here rather than at flush time because the *old* row has to be
    # marked superseded first: the partial unique index allows only one active calibration per
    # patient and device, and it is checked per statement. Inserting the new row while the old
    # one is still active is a unique violation. The self-FK is DEFERRABLE INITIALLY DEFERRED
    # so the old row may point at the new id before that row exists.
    new_calibration_id = uuid.uuid4()

    if superseded is not None:
        # The only mutation permitted on a calibration row. The database trigger
        # tera_calibration_history_guard rejects any change to the baseline itself, so this
        # cannot quietly grow into a rewrite of history.
        superseded.status = CalibrationStatus.SUPERSEDED
        superseded.superseded_by_id = new_calibration_id
        superseded.superseded_at = now
        session.flush()

    new_calibration = Calibration(
        id=new_calibration_id,
        patient_id=patient_id,
        device_profile_id=device_profile_id,
        reference_cuff_reading_id=reference_cuff_reading_id,
        baseline_mean_ms=baseline.mean_ms,
        baseline_sd_ms=baseline.sd_ms,
        n_sessions=baseline.n_sessions,
        status=CalibrationStatus.ACTIVE,
        established_at=now,
        synthetic=synthetic,
    )
    session.add(new_calibration)
    session.flush()

    for source_session_id, ptt in source_ptts.items():
        session.add(
            CalibrationSourceSession(
                calibration_id=new_calibration.id,
                session_id=source_session_id,
                session_ptt_ms=ptt,
            )
        )

    session.flush()
    return EstablishedCalibration(
        calibration=new_calibration,
        superseded=superseded,
        source_session_ptts=source_ptts,
    )


def _collect_session_ptts(
    session: Session,
    *,
    session_ids: list[uuid.UUID],
    patient_id: uuid.UUID,
    device_profile_id: uuid.UUID,
    settings: Settings,
) -> dict[uuid.UUID, float]:
    """Load the named sessions and reduce each to its trimmed-mean PTT.

    Every session must be accepted, belong to this patient, and have been captured on this
    device profile. Invariant 4 binds a calibration to a device: a baseline built from another
    handset's timing characteristics is not a baseline for this one.
    """
    rows = (
        session.execute(select(MeasurementSession).where(MeasurementSession.id.in_(session_ids)))
        .scalars()
        .all()
    )
    found = {row.id: row for row in rows}

    missing = [str(sid) for sid in session_ids if sid not in found]
    if missing:
        raise CalibrationError("session_ids", f"unknown session(s): {', '.join(missing)}")

    ptts: dict[uuid.UUID, float] = {}
    for session_id in session_ids:
        row = found[session_id]

        if row.status is not SessionStatus.COMPLETED:
            raise CalibrationError(
                "session_ids",
                f"session {session_id} was rejected and cannot contribute to a baseline",
            )
        if row.device_profile_id != device_profile_id:
            raise CalibrationError(
                "session_ids",
                f"session {session_id} was captured on a different device profile; a "
                f"calibration is bound to one device (invariant 4)",
            )
        if row.episode.patient_id != patient_id:
            raise CalibrationError(
                "session_ids", f"session {session_id} belongs to a different patient"
            )
        if not row.ptt_ms:
            raise CalibrationError(
                "session_ids", f"session {session_id} carries no beat intervals"
            )

        ptts[session_id] = trimmed_session_ptt(list(row.ptt_ms), settings.deviation).value_ms

    return ptts
