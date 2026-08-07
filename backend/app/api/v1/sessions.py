"""Session nonce and session ingest (BUILD_SPEC 4.2).

The submission contract, verbatim from the spec:

    201  { "trend": { "direction": "stable", "magnitude_sd": 0.4,
                      "confidence": 0.81, "calibration_id": "..." } }
    409  duplicate session_id — return the stored result unchanged
    422  payload failed validation
    428  nonce absent, expired, or already used
    429  rate limit exceeded
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import (
    HTTP_422_UNPROCESSABLE,
    HTTP_428_PRECONDITION_REQUIRED,
    DbDep,
    PrincipalDep,
    SettingsDep,
    ingest_rate_limit,
    load_episode,
    nonce_rate_limit,
)
from app.logging_config import get_logger
from app.models import AuditAction, DeviceProfile, MeasurementSession
from app.schemas.common import SyntheticFlag
from app.schemas.session import NonceOut, SessionAccepted, SessionDetailOut, SessionSubmit
from app.security.nonce import NonceError, consume_nonce, issue_nonce
from app.services import audit, ingest
from app.services.ingest import PayloadRejected

router = APIRouter(prefix="/sessions", tags=["sessions"])
log = get_logger(__name__)


@router.post(
    "/nonce",
    response_model=NonceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Issue a single-use nonce with a short TTL",
)
def create_nonce(
    db: DbDep,
    settings: SettingsDep,
    principal: Annotated[object, Depends(nonce_rate_limit)],
) -> NonceOut:
    """Mint a nonce bound to this token subject."""
    value, expires_at = issue_nonce(
        db, issued_to=principal.subject, settings=settings.security
    )
    audit.record(db, principal=principal, action=AuditAction.NONCE_ISSUED)
    db.commit()
    return NonceOut(nonce=value, expires_at=expires_at)


@router.post(
    "",
    response_model=SessionAccepted,
    status_code=status.HTTP_201_CREATED,
    summary="Submit an accepted or rejected session",
    responses={
        409: {"description": "Duplicate session_id — the stored result is returned unchanged."},
        422: {"description": "Payload failed validation."},
        428: {"description": "Nonce absent, expired or already used."},
        429: {"description": "Rate limit exceeded."},
    },
)
def submit_session(
    body: SessionSubmit,
    db: DbDep,
    settings: SettingsDep,
    principal: Annotated[object, Depends(ingest_rate_limit)],
    response: Response,
    x_session_nonce: Annotated[str | None, Header(alias="X-Session-Nonce")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """Ingest one session.

    Order matters. Idempotency is checked **before** the nonce is spent, so a client retrying
    after a dropped response gets its stored result rather than a 428 for a nonce it already
    used successfully.
    """
    _check_idempotency_key(idempotency_key, body.session_id)

    existing = ingest.find_existing(db, body.session_id)
    if existing is not None:
        # BUILD_SPEC 4.2: "409 duplicate session_id — return the stored result unchanged."
        # Unusual for an idempotent endpoint (200/201 replay is more common) but the spec is
        # explicit, so the stored result is returned with a 409 status.
        audit.record(
            db,
            principal=principal,
            action=AuditAction.SESSION_DUPLICATE_REPLAYED,
            target=existing.id,
        )
        db.commit()
        log.info("session_duplicate_replayed", extra={"session_id": str(existing.id)})
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ingest.replay_stored(existing).model_dump(mode="json"),
        )

    try:
        consume_nonce(db, value=x_session_nonce, presented_by=principal.subject)
    except NonceError as exc:
        raise HTTPException(
            status_code=HTTP_428_PRECONDITION_REQUIRED, detail=str(exc)
        ) from exc

    episode = load_episode(body.episode_id, principal, db)

    device_profile = db.get(DeviceProfile, body.device_profile_id)
    if device_profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="device profile not found"
        )
    if device_profile.patient_id != episode.patient_id:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail="device profile belongs to a different patient",
        )

    try:
        result = ingest.submit(
            db,
            payload=body,
            episode=episode,
            device_profile=device_profile,
            settings=settings,
        )
    except PayloadRejected as exc:
        db.rollback()
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail={
                "detail": "session payload failed validation",
                "violations": [
                    {"field": v.field, "message": v.message} for v in exc.violations
                ],
            },
        ) from exc

    audit.record(
        db, principal=principal, action=AuditAction.SESSION_SUBMITTED, target=result.session.id
    )
    db.commit()

    # Structured log: ids, version, rates, gate outcome, timing. No PTT values, no pressures.
    log.info(
        "session_ingested",
        extra={
            "session_id": str(result.session.id),
            "device_profile_id": str(result.session.device_profile_id),
            "model_version": result.session.model_version,
            "accel_rate_hz": result.session.quality.get("accel_rate_hz"),
            "camera_fps": result.session.quality.get("camera_fps"),
            "gate_outcome": result.session.status.value,
            "estimate_produced": result.estimate is not None,
        },
    )

    response.status_code = status.HTTP_201_CREATED
    return result.response


@router.get(
    "/{session_id}",
    response_model=SessionDetailOut,
    summary="Session detail, including quality metrics and what the gate checked",
)
def get_session(
    session_id: uuid.UUID, db: DbDep, principal: PrincipalDep
) -> SessionDetailOut:
    """Drives the Phase 2 session-detail screen."""
    stored = db.get(MeasurementSession, session_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")

    load_episode(stored.episode_id, principal, db)  # authorisation, result unused

    return SessionDetailOut(
        session_id=stored.id,
        episode_id=stored.episode_id,
        device_profile_id=stored.device_profile_id,
        started_at=stored.started_at,
        received_at=stored.received_at,
        posture=stored.posture,
        status=stored.status,
        model_version=stored.model_version,
        n_beats_total=stored.n_beats_total,
        n_beats_usable=stored.n_beats_usable,
        quality=stored.quality,
        synthetic=stored.synthetic,
        synthetic_notice=SyntheticFlag.notice_for(stored.synthetic),
        trend=(
            ingest.build_estimate_out(stored.estimate) if stored.estimate is not None else None
        ),
        rejection=(
            ingest.build_rejection_out(stored.rejection_reason)
            if stored.rejection_reason is not None
            else None
        ),
    )


def _check_idempotency_key(idempotency_key: str | None, session_id: uuid.UUID) -> None:
    """The Idempotency-Key header must be the session id (BUILD_SPEC 4.2).

    Requiring them to match means a client cannot accidentally deduplicate two different
    captures under one key, or submit one capture twice under two keys.
    """
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required and must equal session_id",
        )
    try:
        parsed = uuid.UUID(idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must be a UUID equal to session_id",
        ) from exc
    if parsed != session_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must equal session_id",
        )
