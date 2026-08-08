"""Token issue and refresh.

DEVIATION from BUILD_SPEC 4.2's endpoint table, which omits an auth endpoint while 4.5 mandates
OAuth2 with access and refresh tokens. Recorded in docs/decisions.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from app.api.deps import DbDep, SettingsDep
from app.logging_config import get_logger
from app.models import AppUser, AuditAction
from app.schemas.auth import RefreshRequest, TokenResponse
from app.security.passwords import verify_password
from app.security.tokens import Principal, TokenError, decode_token, issue_token
# Aliased: the route handler below is also called `refresh_tokens`, and at module scope the
# function would shadow the module. Renaming the handler instead would change its operationId
# in the OpenAPI schema, which is part of the published contract.
from app.services import audit
from app.services import refresh_tokens as refresh_token_service

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


@router.post("/token", response_model=TokenResponse, summary="Exchange credentials for tokens")
def issue_tokens(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DbDep,
    settings: SettingsDep,
) -> TokenResponse:
    """OAuth2 password grant."""
    user = db.execute(
        select(AppUser).where(AppUser.subject == form.username)
    ).scalar_one_or_none()

    # Same response for an unknown subject and a wrong password, so the endpoint cannot be used
    # to enumerate accounts.
    if user is None or not verify_password(form.password, user.password_hash):
        log.info("auth_failed", extra={"subject_present": user is not None})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    principal = _principal_for(user)

    # The refresh token is recorded before it is signed, so a token can never exist without the
    # row that allows it to be revoked.
    issued = refresh_token_service.issue(
        db,
        user_id=user.id,
        expires_at=datetime.now(tz=timezone.utc)
        + timedelta(days=settings.security.refresh_token_ttl_days),
    )
    tokens = _mint(principal, settings, refresh_jti=issued.jti)

    audit.record(db, principal=principal, action=AuditAction.AUTH_TOKEN_ISSUED, target=user.id)
    db.commit()
    return tokens


@router.post("/refresh", response_model=TokenResponse, summary="Exchange a refresh token")
def refresh_tokens(
    body: RefreshRequest, db: DbDep, settings: SettingsDep
) -> TokenResponse:
    """Trade a refresh token for a fresh pair.

    The refresh token is re-checked against the user row rather than trusted on its claims
    alone, so a token minted for an account that has since been removed stops working.
    """
    try:
        principal = decode_token(
            body.refresh_token, expected_type="refresh", settings=settings.security
        )
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc

    user = db.get(AppUser, principal.user_id)
    if user is None or user.subject != principal.subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token subject no longer exists"
        )

    if not principal.jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token carries no identifier and cannot be rotated",
        )

    expires_at = datetime.now(tz=timezone.utc) + timedelta(
        days=settings.security.refresh_token_ttl_days
    )
    try:
        issued = refresh_token_service.rotate(
            db, jti=principal.jti, user_id=user.id, expires_at=expires_at
        )
    except refresh_token_service.RefreshTokenError as exc:
        db.commit()  # the family revocation, if any, must survive the failed request
        if exc.family_revoked:
            log.warning(
                "refresh_token_family_revoked",
                extra={"user_id": str(user.id), "reason": "reuse_or_mismatch"},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.message) from exc

    tokens = _mint(_principal_for(user), settings, refresh_jti=issued.jti)
    db.commit()
    return tokens


def _principal_for(user: AppUser) -> Principal:
    return Principal(
        subject=user.subject,
        role=user.role,
        user_id=user.id,
        patient_id=user.patient_id,
        clinic_id=user.clinic_id,
    )


def _mint(
    principal: Principal, settings: SettingsDep, *, refresh_jti: str
) -> TokenResponse:
    """Build the token pair.

    ``refresh_jti`` must be the id of an already-persisted ``refresh_token`` row: the claim and
    the record have to agree or the token could never be rotated or revoked.
    """
    access, _ = issue_token(
        principal=principal, token_type="access", settings=settings.security
    )
    refresh, _ = issue_token(
        principal=principal,
        token_type="refresh",
        settings=settings.security,
        jti=refresh_jti,
    )
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.security.access_token_ttl_minutes * 60,
        role=principal.role,
    )
