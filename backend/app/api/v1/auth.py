"""Token issue and refresh.

DEVIATION from BUILD_SPEC 4.2's endpoint table, which omits an auth endpoint while 4.5 mandates
OAuth2 with access and refresh tokens. Recorded in docs/decisions.md.
"""

from __future__ import annotations

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
from app.services import audit

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
    tokens = _mint(principal, settings)

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

    return _mint(_principal_for(user), settings)


def _principal_for(user: AppUser) -> Principal:
    return Principal(
        subject=user.subject,
        role=user.role,
        user_id=user.id,
        patient_id=user.patient_id,
        clinic_id=user.clinic_id,
    )


def _mint(principal: Principal, settings: SettingsDep) -> TokenResponse:
    access, _ = issue_token(
        principal=principal, token_type="access", settings=settings.security
    )
    refresh, _ = issue_token(
        principal=principal, token_type="refresh", settings=settings.security
    )
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.security.access_token_ttl_minutes * 60,
        role=principal.role,
    )
