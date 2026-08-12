"""FastAPI dependencies: authentication, authorisation and rate limiting."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import AppUser, MonitoringEpisode, UserRole
from app.security.ratelimit import limiter
from app.security.tokens import Principal, TokenError, decode_token

#: HTTP 428 Precondition Required — the nonce contract (BUILD_SPEC 4.2).
HTTP_428_PRECONDITION_REQUIRED = 428

#: Starlette has renamed its 422 constant and deprecated the old spelling. The number is the
#: stable part of the contract (BUILD_SPEC 4.2 names it explicitly), so it is written literally
#: here rather than tracking whichever alias the framework currently prefers.
HTTP_422_UNPROCESSABLE = 422

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token", auto_error=False)

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[Session, Depends(get_db)]


def get_principal(
    token: Annotated[str | None, Depends(oauth2_scheme)],
    settings: SettingsDep,
) -> Principal:
    """Resolve the caller from the bearer token."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="an access token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_token(token, expected_type="access", settings=settings.security)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_roles(*roles: UserRole):
    """Dependency factory restricting a route to the given roles."""

    def _dependency(principal: PrincipalDep) -> Principal:
        if principal.role not in roles and not principal.is_admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this endpoint requires one of: {', '.join(r.value for r in roles)}",
            )
        return principal

    return _dependency


def load_episode(
    episode_id: uuid.UUID, principal: Principal, db: Session
) -> MonitoringEpisode:
    """Load an episode the caller is entitled to see.

    BUILD_SPEC 4.5: "Clinician access scoped to episodes where they are the reviewing
    professional." A patient sees only their own.

    Cross-tenant access returns 404 — see :func:`assert_owns_or_404` for why.
    """
    episode = db.get(MonitoringEpisode, episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="episode not found")

    if principal.is_admin:
        return episode
    if principal.is_patient and episode.patient_id == principal.patient_id:
        return episode
    if principal.is_clinician and episode.reviewing_clinician_id == principal.user_id:
        return episode

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="episode not found")


def require_patient(principal: Principal) -> uuid.UUID:
    """The patient id behind the caller's token, or 403.

    Every PHR route needs this and none of them may take a patient id from the request body: the
    id comes from the token, so a profile cannot be edited against somebody else's record by
    changing a payload.
    """
    if principal.patient_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a patient account has a health record",
        )
    return principal.patient_id


def assert_patient_scope(principal: Principal, patient_id: uuid.UUID) -> None:
    """A patient principal may only act on their own patient record.

    Returns 403, not 404, and that is deliberate: this is a pure comparison against the
    ``patient_id`` in the caller's own token, made before any database lookup. The answer is
    identical for a patient record that exists and one that does not, so nothing is disclosed.

    Use this when the caller *names* another patient in a request body. Use
    :func:`assert_owns_or_404` when the caller has already caused a row to be loaded.
    """
    if principal.is_admin:
        return
    if principal.is_patient and principal.patient_id == patient_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="this token may not act on that patient record",
    )


def assert_owns_or_404(
    principal: Principal, owner_patient_id: uuid.UUID, *, resource: str
) -> None:
    """Deny access to another patient's row by claiming it does not exist.

    The rule, applied consistently across the API:

    * **404** when the caller is not entitled to know the resource exists — anything belonging
      to another patient. Returning 403 here would confirm that the id names a real row, which
      is a disclosure about someone else's care even when no field is returned. An attacker
      with a list of candidate ids could separate the real from the invented.
    * **403** when the resource is not secret but the caller lacks authority — a patient
      hitting an admin-only endpoint, or asking for the clinician summary of their own episode.
      They already know it exists; refusing tells them nothing new, and 404 would be a lie that
      makes the client harder to debug.

    A clinician is not covered by this helper: their scope is the episodes they review, which
    :func:`load_episode` resolves.
    """
    if principal.is_admin:
        return
    if principal.is_patient and principal.patient_id == owner_patient_id:
        return
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"{resource} not found"
    )


def resolve_user(db: Session, principal: Principal) -> AppUser:
    """Load the AppUser row behind a token, or 401 if it has since been removed."""
    user = db.get(AppUser, principal.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token subject no longer exists"
        )
    return user


class RateLimit:
    """Per-token and, where a patient is in scope, per-patient rate limiting.

    BUILD_SPEC 4.5 requires both dimensions on ingest and summary endpoints. The per-patient
    key matters because several devices can hold tokens for the same patient.
    """

    def __init__(self, bucket: str, per_token: str, per_patient: str | None = None) -> None:
        self._bucket = bucket
        self._per_token_setting = per_token
        self._per_patient_setting = per_patient

    def __call__(
        self, request: Request, principal: PrincipalDep, settings: SettingsDep
    ) -> Principal:
        token_limit = getattr(settings.security, self._per_token_setting)
        self._enforce(f"{self._bucket}:token:{principal.subject}", token_limit)

        if self._per_patient_setting and principal.patient_id is not None:
            patient_limit = getattr(settings.security, self._per_patient_setting)
            self._enforce(f"{self._bucket}:patient:{principal.patient_id}", patient_limit)

        del request
        return principal

    @staticmethod
    def _enforce(key: str, limit: int) -> None:
        decision = limiter.check(key, limit)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate limit exceeded",
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )


ingest_rate_limit = RateLimit(
    "ingest",
    per_token="ingest_rate_limit_per_token_per_hour",
    per_patient="ingest_rate_limit_per_patient_per_hour",
)
summary_rate_limit = RateLimit("summary", per_token="summary_rate_limit_per_token_per_hour")
nonce_rate_limit = RateLimit("nonce", per_token="nonce_rate_limit_per_token_per_hour")
