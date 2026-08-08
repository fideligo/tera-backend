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

from app.api.deps import (
    HTTP_422_UNPROCESSABLE,
    DbDep,
    PrincipalDep,
    SettingsDep,
    require_roles,
)
from app.logging_config import get_logger
from app.models import AppUser, AuditAction, Patient, UserRole
from app.schemas.auth import (
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.security.passwords import hash_password, verify_password
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
        # Recorded whether or not the account exists. Failures against accounts that do not
        # exist are the signature of credential stuffing, and dropping them would blind the
        # audit trail to the attack it most needs to show.
        audit.record_unauthenticated(
            db, actor=form.username, action=AuditAction.AUTH_LOGIN_FAILED
        )
        db.commit()
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
        if exc.family_revoked:
            # A security incident, not an ordinary expiry: someone replayed a token that had
            # already been rotated. Recorded distinctly so it can be found later.
            audit.record(
                db,
                principal=principal,
                action=AuditAction.AUTH_REFRESH_REUSE_DETECTED,
                target=user.id,
            )
            log.warning(
                "refresh_token_family_revoked",
                extra={"user_id": str(user.id), "reason": "reuse_or_mismatch"},
            )
        db.commit()  # the family revocation and its audit entry must survive the failed request
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.message) from exc

    tokens = _mint(_principal_for(user), settings, refresh_jti=issued.jti)
    audit.record(
        db, principal=principal, action=AuditAction.AUTH_TOKEN_REFRESHED, target=user.id
    )
    db.commit()
    return tokens


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="End a session by revoking its refresh token",
)
def logout(body: LogoutRequest, db: DbDep, principal: PrincipalDep, settings: SettingsDep) -> None:
    """Revoke one session, or every session this account holds.

    The access token is not revoked and does not need to be: it expires in minutes, and
    maintaining a denylist for it would mean a database read on every authenticated request to
    close a window that closes itself. What logout must guarantee is that the *refresh* token
    stops working, because that is the one with a fortnight of life in it.

    Idempotent. Logging out twice, or with a token that has already expired, is a 204 — a client
    clearing its local state should not have to handle an error to do so.
    """
    if body.all_sessions:
        count = refresh_token_service.revoke_all_for_user(
            db, user_id=principal.user_id, reason="logout_all"
        )
        log.info(
            "logout_all_sessions",
            extra={"user_id": str(principal.user_id), "sessions_ended": count},
        )
    else:
        try:
            token_principal = decode_token(
                body.refresh_token or "", expected_type="refresh", settings=settings.security
            )
        except TokenError:
            # An unreadable or expired token is already not a usable session. Nothing to do.
            db.commit()
            return None

        if token_principal.user_id != principal.user_id:
            # Revoking someone else's session on the strength of holding their refresh token
            # would be a denial-of-service primitive.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="that refresh token belongs to a different account",
            )

        if token_principal.jti:
            refresh_token_service.revoke(db, jti=token_principal.jti, reason="logout")

    audit.record(
        db, principal=principal, action=AuditAction.AUTH_LOGOUT, target=principal.user_id
    )
    db.commit()
    return None


@router.get("/me", response_model=UserOut, summary="The account behind the current token")
def read_me(db: DbDep, principal: PrincipalDep) -> UserOut:
    """Identity only.

    No clinical content, so a client can establish who it is signed in as without touching a
    patient record. ``active_sessions`` lets a user see whether a device they no longer have is
    still able to refresh.
    """
    user = db.get(AppUser, principal.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token subject no longer exists"
        )

    return UserOut(
        id=user.id,
        subject=user.subject,
        role=user.role,
        clinic_id=user.clinic_id,
        patient_id=user.patient_id,
        created_at=user.created_at,
        active_sessions=refresh_token_service.active_session_count(db, user_id=user.id),
    )


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account (admin only)",
)
def register(
    body: RegisterRequest,
    db: DbDep,
    principal: Annotated[Principal, Depends(require_roles(UserRole.ADMIN))],
) -> UserOut:
    """Create a login.

    Admin-only, and there is no self-service path. The proposal describes enrolment as
    clinic-initiated: a patient is enrolled into a monitoring episode when their treatment is
    adjusted, by the clinic. A public sign-up form would let anyone create an account holding
    clinical data with no clinic behind it.
    """
    existing = db.execute(
        select(AppUser).where(AppUser.subject == body.subject)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="that subject is already registered"
        )

    if body.patient_id is not None and db.get(Patient, body.patient_id) is None:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail="patient_id does not name an existing patient record",
        )

    user = AppUser(
        subject=body.subject,
        password_hash=hash_password(body.password),
        role=body.role,
        clinic_id=body.clinic_id,
        patient_id=body.patient_id,
    )
    db.add(user)
    db.flush()

    audit.record(db, principal=principal, action=AuditAction.USER_REGISTERED, target=user.id)
    db.commit()

    # Role and ids only. The password never appears here or in the log.
    log.info(
        "user_registered",
        extra={"created_user_id": str(user.id), "role": user.role.value},
    )

    return UserOut(
        id=user.id,
        subject=user.subject,
        role=user.role,
        clinic_id=user.clinic_id,
        patient_id=user.patient_id,
        created_at=user.created_at,
        active_sessions=0,
    )


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
