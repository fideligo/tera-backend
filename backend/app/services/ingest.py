"""Session ingest — the path a capture takes from the handset into the record.

This is where invariants 2, 3, 4 and 7 all have to hold at once, so the order of operations
matters:

1. plausibility gate (the backend does not trust the client)
2. persist the session **whatever its outcome** — invariant 3, rejected sessions are retained
3. resolve the calibration in force *at capture time* — invariant 4
4. produce an estimate only when the calibration and the signal both support one — invariant 7
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import (
    Calibration,
    DeviationState,
    DeviceProfile,
    MeasurementSession,
    MonitoringEpisode,
    RejectionReason,
    SessionStatus,
    TrendDirection,
    TrendEstimate,
)
from app.schemas.session import (
    NextAction,
    RejectionOut,
    SessionAccepted,
    SessionSubmit,
    TrendEstimateOut,
)
from app.services import language, protocol
from app.services import calibration as calibration_service
from app.services.deviation import Baseline, evaluate
from app.services.plausibility import Violation, check_session_payload


class PayloadRejected(Exception):
    """The payload failed the server-side plausibility gate. Maps to HTTP 422."""

    def __init__(self, violations: list[Violation]) -> None:
        super().__init__("session payload failed validation")
        self.violations = violations


@dataclass(frozen=True)
class IngestResult:
    session: MeasurementSession
    estimate: TrendEstimate | None
    response: SessionAccepted


def find_existing(session: Session, session_id: uuid.UUID) -> MeasurementSession | None:
    """Idempotency (BUILD_SPEC 4.2): the device-generated session id is the key."""
    return session.get(MeasurementSession, session_id)


def submit(
    db: Session,
    *,
    payload: SessionSubmit,
    episode: MonitoringEpisode,
    device_profile: DeviceProfile,
    settings: Settings,
    received_at: datetime | None = None,
) -> IngestResult:
    """Ingest one session and return the response the device should see.

    ``received_at`` is for the seeder only, so a demonstration episode has upload times that
    match its capture times. The HTTP route never passes it: a client must not be able to
    backdate when the server received something.
    """
    min_beats = protocol.min_beat_count(episode, settings)

    violations = check_session_payload(
        ptt_ms=payload.ptt_ms,
        n_beats_total=payload.n_beats_total,
        n_beats_usable=payload.n_beats_usable,
        quality=payload.quality.model_dump(exclude_none=True),
        status_is_completed=payload.status is SessionStatus.COMPLETED,
        min_usable_beats=min_beats,
        profile_accel_rate_hz=device_profile.accel_rate_hz,
        profile_camera_fps=device_profile.camera_fps,
        settings=settings.plausibility,
    )
    if violations:
        raise PayloadRejected(violations)

    in_force = calibration_service.resolve_at(
        db,
        patient_id=episode.patient_id,
        device_profile_id=device_profile.id,
        at=payload.started_at,
    )

    # A device that names a calibration must name the right one. Silently overriding it would
    # hide a client that has drifted out of step with the server's view of the baseline.
    if payload.calibration_id is not None and (
        in_force is None or in_force.id != payload.calibration_id
    ):
        raise PayloadRejected(
            [
                Violation(
                    "calibration_id",
                    "does not match the calibration in force at started_at. The server "
                    "resolves the calibration by capture time (invariant 4); resubmit without "
                    "this field or refresh the device's calibration state.",
                )
            ]
        )

    stored = MeasurementSession(
        id=payload.session_id,
        episode_id=episode.id,
        device_profile_id=device_profile.id,
        calibration_id=in_force.id if in_force is not None else None,
        model_version=payload.model_version,
        started_at=payload.started_at,
        posture=payload.posture,
        status=payload.status,
        rejection_reason=payload.rejection_reason,
        n_beats_total=payload.n_beats_total,
        n_beats_usable=payload.n_beats_usable,
        ptt_ms=list(payload.ptt_ms),
        quality=payload.quality.model_dump(exclude_none=True),
        synthetic=payload.synthetic,
        **({"received_at": received_at} if received_at is not None else {}),
    )
    db.add(stored)
    db.flush()

    # Invariant 3 — the session is on the record before any decision about an estimate.
    if payload.status is SessionStatus.REJECTED:
        return IngestResult(
            session=stored,
            estimate=None,
            response=_rejected_response(stored, payload.rejection_reason),
        )

    # Invariant 7 — no calibration in force means no estimate. The capture is kept; it is a
    # calibration session, and the correct next step is a cuff reading to anchor a baseline.
    if in_force is None:
        return IngestResult(
            session=stored,
            estimate=None,
            response=SessionAccepted(
                session_id=stored.id,
                status=stored.status,
                synthetic=stored.synthetic,
                synthetic_notice=SessionAccepted.notice_for(stored.synthetic),
                trend=None,
                rejection=None,
                action=NextAction(
                    kind="cuff_reading_requested",
                    message=language.ACTION_CUFF_REQUESTED_NO_CALIBRATION,
                ),
            ),
        )

    estimate = _produce_estimate(
        db,
        stored=stored,
        in_force=in_force,
        episode=episode,
        settings=settings,
        min_beats=min_beats,
    )

    return IngestResult(
        session=stored,
        estimate=estimate,
        response=_accepted_response(stored, estimate),
    )


def _produce_estimate(
    db: Session,
    *,
    stored: MeasurementSession,
    in_force: Calibration,
    episode: MonitoringEpisode,
    settings: Settings,
    min_beats: int,
) -> TrendEstimate:
    """Run the deviation engine and persist the estimate."""
    baseline = Baseline(
        mean_ms=in_force.baseline_mean_ms,
        sd_ms=in_force.baseline_sd_ms,
        n_sessions=in_force.n_sessions,
    )
    window_hours = protocol.persistence_window_hours(episode, settings)
    prior_direction = _prior_deviating_direction(
        db,
        episode_id=episode.id,
        before=stored.started_at,
        window=timedelta(hours=window_hours),
    )

    _session_ptt, result = evaluate(
        ptt_ms=list(stored.ptt_ms),
        baseline=baseline,
        quality=stored.quality,
        n_usable_beats=stored.n_beats_usable,
        deviation_k=protocol.deviation_k(episode, settings),
        min_usable_beats=min_beats,
        prior_deviating_direction=prior_direction,
        settings=settings.deviation,
    )

    estimate = TrendEstimate(
        session_id=stored.id,
        calibration_id=in_force.id,
        direction=result.direction,
        magnitude_sd=result.magnitude_sd,
        confidence=result.confidence,
        deviation_state=result.deviation_state,
        synthetic=stored.synthetic,
    )
    db.add(estimate)
    db.flush()
    return estimate


def _prior_deviating_direction(
    db: Session, *, episode_id: uuid.UUID, before: datetime, window: timedelta
) -> TrendDirection | None:
    """Direction of the most recent deviating estimate inside the persistence window.

    BUILD_SPEC 4.3: "``persistent`` when a repeat session within the configured window also
    deviates. A single deviating session never triggers a cuff request."
    """
    stmt = (
        select(TrendEstimate.direction)
        .join(MeasurementSession, MeasurementSession.id == TrendEstimate.session_id)
        .where(
            MeasurementSession.episode_id == episode_id,
            MeasurementSession.started_at < before,
            MeasurementSession.started_at >= before - window,
            TrendEstimate.direction != TrendDirection.STABLE,
        )
        .order_by(MeasurementSession.started_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def build_estimate_out(estimate: TrendEstimate) -> TrendEstimateOut:
    """Render a stored estimate for the API. Never contains a pressure value (invariant 1)."""
    return TrendEstimateOut(
        calibration_id=estimate.calibration_id,
        direction=estimate.direction,
        magnitude_sd=estimate.magnitude_sd,
        confidence=estimate.confidence,
        deviation_state=estimate.deviation_state,
        interpretation=language.DIRECTION_WORDING[estimate.direction],
    )


def build_rejection_out(reason: RejectionReason) -> RejectionOut:
    return RejectionOut(reason=reason, message=language.REJECTION_WORDING[reason])


def _accepted_response(
    stored: MeasurementSession, estimate: TrendEstimate
) -> SessionAccepted:
    if estimate.deviation_state is DeviationState.PERSISTENT:
        action = NextAction(
            kind="cuff_reading_requested", message=language.ACTION_CUFF_REQUESTED
        )
    elif estimate.deviation_state is DeviationState.POSSIBLE:
        # A single deviating session asks for a repeat, never for a cuff (BUILD_SPEC 4.3).
        action = NextAction(
            kind="repeat_session_suggested", message=language.ACTION_REPEAT_SUGGESTED
        )
    else:
        action = NextAction(kind="none", message=language.ACTION_NONE)

    return SessionAccepted(
        session_id=stored.id,
        status=stored.status,
        synthetic=stored.synthetic,
        synthetic_notice=SessionAccepted.notice_for(stored.synthetic),
        trend=build_estimate_out(estimate),
        rejection=None,
        action=action,
    )


def _rejected_response(
    stored: MeasurementSession, reason: RejectionReason | None
) -> SessionAccepted:
    # The status/reason CHECK constraint makes this unreachable, but a None here would produce a
    # rejected session with no explanation, which invariant 3 exists to prevent.
    assert reason is not None, "a rejected session must carry a rejection reason"

    if reason is RejectionReason.RED_FLAG_REPORTED:
        # Invariant 8 — no measurement offered, no estimate displayed. The handset has already
        # shown this locally without waiting for the network.
        action = NextAction(
            kind="seek_emergency_care", message=language.ACTION_SEEK_EMERGENCY_CARE
        )
    else:
        action = NextAction(
            kind="cuff_reading_requested",
            message=language.ACTION_CUFF_REQUESTED_SESSION_UNUSABLE,
        )

    return SessionAccepted(
        session_id=stored.id,
        status=stored.status,
        synthetic=stored.synthetic,
        synthetic_notice=SessionAccepted.notice_for(stored.synthetic),
        trend=None,
        rejection=build_rejection_out(reason),
        action=action,
    )


def replay_stored(stored: MeasurementSession) -> SessionAccepted:
    """Rebuild the response for an already-ingested session.

    BUILD_SPEC 4.2: a duplicate ``session_id`` returns "the stored result unchanged". Rebuilt
    from the stored rows rather than from a cached response body, so a replay cannot diverge
    from what is actually on the record.
    """
    if stored.status is SessionStatus.REJECTED:
        return _rejected_response(stored, stored.rejection_reason)
    if stored.estimate is None:
        return SessionAccepted(
            session_id=stored.id,
            status=stored.status,
            synthetic=stored.synthetic,
            synthetic_notice=SessionAccepted.notice_for(stored.synthetic),
            trend=None,
            rejection=None,
            action=NextAction(
                kind="cuff_reading_requested",
                message=language.ACTION_CUFF_REQUESTED_NO_CALIBRATION,
            ),
        )
    return _accepted_response(stored, stored.estimate)
