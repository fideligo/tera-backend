"""Token issue and refresh.

DEVIATION from BUILD_SPEC 4.2's endpoint table, which omits an auth endpoint while 4.5 mandates
OAuth2 with access and refresh tokens. Recorded in docs/decisions.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
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
from app.models import (
    AppUser,
    AuditAction,
    MonitoringEpisode,
    Patient,
    RefreshToken,
    UserRole,
)
from app.schemas.auth import (
    LogoutRequest,
    RefreshRequest,
    RegisterPatientRequest,
    RegisterPatientResponse,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.security import authlimit
from app.security.passwords import hash_password, verify_password
from app.security.tokens import Principal, TokenError, decode_token, issue_token
# Aliased: the route handler below is also called `refresh_tokens`, and at module scope the
# function would shadow the module. Renaming the handler instead would change its operationId
# in the OpenAPI schema, which is part of the published contract.
from app.services import audit
from app.services import refresh_tokens as refresh_token_service

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


def _client_address(request: Request) -> str:
    """The caller's address, for the coarse per-address limit.

    Deliberately does **not** trust X-Forwarded-For. Behind a proxy that header is authoritative;
    in front of one it is attacker-controlled, and honouring it unconditionally would let anyone
    reset their own limit by inventing an address. If this is ever deployed behind a real proxy,
    configure the proxy headers middleware rather than reading the header here.
    """
    return request.client.host if request.client else "unknown"


def _deny(decision, *, detail: str) -> None:
    """Raise a 429 carrying the headers a well-behaved client needs to back off."""
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={
            "Retry-After": str(decision.retry_after_seconds),
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
        },
    )


@router.post("/token", response_model=TokenResponse, summary="Exchange credentials for tokens")
def issue_tokens(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> TokenResponse:
    """OAuth2 password grant."""
    # Rate limited before the password is checked, and before the user row is even loaded. A
    # limiter that runs after verification still performs the expensive hash comparison on every
    # attempt, which is most of what an attacker is trying to make us do.
    #
    # Keyed on the *attempted* username rather than a resolved user id: at this point there may
    # be no such user, and refusing to count attempts against non-existent accounts would leave
    # credential stuffing — which is mostly attempts against non-existent accounts — unmetered.
    security = settings.security
    username_limit = authlimit.AuthLimit(
        bucket="auth_token_username",
        limit=security.auth_login_limit_per_username,
        window_seconds=security.auth_login_window_seconds,
    )
    address_limit = authlimit.AuthLimit(
        bucket="auth_token_address",
        limit=security.auth_login_limit_per_address,
        window_seconds=security.auth_login_address_window_seconds,
    )

    by_username = authlimit.check(db, username_limit, form.username)
    by_address = authlimit.check(db, address_limit, _client_address(request))
    if not by_username.allowed or not by_address.allowed:
        audit.record_unauthenticated(
            db, actor=form.username, action=AuditAction.AUTH_LOGIN_FAILED
        )
        db.commit()
        log.warning(
            "auth_rate_limited",
            extra={"bucket": "token", "username_ok": by_username.allowed},
        )
        # The same message either way. Saying which limit was hit tells an attacker whether they
        # are being throttled per account or per address, which is a hint about how to spread out.
        _deny(
            by_username if not by_username.allowed else by_address,
            detail="too many sign-in attempts; try again later",
        )

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
    body: RefreshRequest, request: Request, db: DbDep, settings: SettingsDep
) -> TokenResponse:
    """Trade a refresh token for a fresh pair.

    The refresh token is re-checked against the user row rather than trusted on its claims
    alone, so a token minted for an account that has since been removed stops working.

    Rate limited per **token family**, which is the precise unit here: a family is exactly one
    login, and reuse detection already operates at that granularity. Per-address alone would fail
    behind NAT, where an attacker shares an address with legitimate users and the limit either
    lets the attack through or locks out the bystanders. Coarse per-address plus precise
    per-family gives both.
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

    security = settings.security
    family_limit = authlimit.AuthLimit(
        bucket="auth_refresh_family",
        limit=security.auth_refresh_limit_per_family,
        window_seconds=security.auth_refresh_window_seconds,
    )
    address_limit = authlimit.AuthLimit(
        bucket="auth_refresh_address",
        limit=security.auth_refresh_limit_per_address,
        window_seconds=security.auth_refresh_address_window_seconds,
    )

    # The family is read before rotation, because rotation is what would change it. Falling back
    # to the jti keeps an unknown token metered rather than unmetered — the failure mode of a
    # missing row must not be "no limit applies".
    stored = db.execute(
        select(RefreshToken).where(RefreshToken.jti == principal.jti)
    ).scalar_one_or_none()
    family_key = str(stored.family_id) if stored is not None else principal.jti

    by_family = authlimit.check(db, family_limit, family_key)
    by_address = authlimit.check(db, address_limit, _client_address(request))

    if not by_family.allowed:
        # A client hammering one family's tokens is either broken or hostile. Tolerate a few over
        # the line — retries and clock skew are real — and end the login past that, because at
        # that depth it has stopped being plausibly accidental.
        depth = authlimit.breach_depth(db, family_limit, family_key)
        if stored is not None and depth >= security.auth_refresh_breach_revoke_threshold:
            refresh_token_service.revoke_family(
                db, family_id=stored.family_id, reason="refresh_rate_limit_breached"
            )
            audit.record(
                db,
                principal=principal,
                action=AuditAction.AUTH_REFRESH_REUSE_DETECTED,
                target=user.id,
            )
            db.commit()
            log.warning(
                "refresh_family_revoked_for_rate_limit",
                extra={"user_id": str(user.id), "breach_depth": depth},
            )
        _deny(by_family, detail="too many refresh attempts; sign in again")

    if not by_address.allowed:
        _deny(by_address, detail="too many refresh attempts; try again later")

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
    "/register-patient",
    response_model=RegisterPatientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Self-registration for the standalone app",
)
def register_patient(
    body: RegisterPatientRequest,
    request: Request,
    db: DbDep,
    settings: SettingsDep,
) -> RegisterPatientResponse:
    """Create a patient account, its patient record and its first monitoring episode.

    B2C PIVOT. `/register` above is admin-only because enrolment used to be clinic-initiated. In a
    standalone app there is no clinic to initiate it, so all three rows are created here, in one
    transaction — a patient account without a patient record violates the database CHECK, and a
    patient without an episode has nowhere to record anything, so a partial success is worse than
    a failure.

    `clinic_id` is left null on both the user and the patient. A placeholder would be a clinic
    affiliation that does not exist, written into clinical records.

    `reviewing_clinician_id` is left null on the episode. The column was optional from 0001, so
    this needs no schema change: an episode has always been able to exist before anyone was
    assigned to review it.

    **This is the only unauthenticated route that writes**, so it is rate limited per address
    before anything is created.
    """
    security = settings.security
    address_limit = authlimit.AuthLimit(
        bucket="auth_register_address",
        limit=security.auth_register_limit_per_address,
        window_seconds=security.auth_register_address_window_seconds,
    )
    by_address = authlimit.check(db, address_limit, _client_address(request))
    if not by_address.allowed:
        db.commit()
        log.warning("auth_rate_limited", extra={"bucket": "register_patient"})
        _deny(by_address, detail="too many sign-up attempts; try again later")

    existing = db.execute(
        select(AppUser).where(AppUser.subject == body.subject)
    ).scalar_one_or_none()
    if existing is not None:
        # Same shape as /register's conflict. This does leak that a subject is taken, which any
        # sign-up form does by construction — the alternative is accepting a registration that
        # silently does nothing.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="that subject is already registered"
        )

    now = datetime.now(tz=timezone.utc)

    # A pseudonym, not an identifier derived from the subject. BUILD_SPEC 4.1 has nowhere to put
    # a name, and deriving the pseudonym from an email address would put one there sideways.
    patient = Patient(
        pseudonym=f"TERA-{uuid.uuid4().hex[:12].upper()}",
        clinic_id=None,
        enrolled_at=now,
        synthetic=False,
    )
    db.add(patient)
    db.flush()

    user = AppUser(
        subject=body.subject,
        password_hash=hash_password(body.password),
        role=UserRole.PATIENT,
        clinic_id=None,
        patient_id=patient.id,
        synthetic=False,
    )
    db.add(user)
    db.flush()

    episode = MonitoringEpisode(
        patient_id=patient.id,
        reviewing_clinician_id=None,
        started_at=now,
        ended_at=None,
        # Empty: every threshold falls back to app.config. A self-registered patient has no
        # clinician to have chosen a per-episode protocol, and inventing one would present an
        # engineering default as a clinical decision.
        protocol_params={},
        synthetic=False,
    )
    db.add(episode)
    db.flush()

    principal = _principal_for(user)
    issued = refresh_token_service.issue(
        db,
        user_id=user.id,
        expires_at=now + timedelta(days=security.refresh_token_ttl_days),
    )
    tokens = _mint(principal, settings, refresh_jti=issued.jti)

    audit.record(db, principal=principal, action=AuditAction.USER_REGISTERED, target=user.id)
    audit.record(db, principal=principal, action=AuditAction.AUTH_TOKEN_ISSUED, target=user.id)
    db.commit()

    # Ids and role only. No subject, no password, no pseudonym.
    log.info("patient_self_registered", extra={"created_user_id": str(user.id)})

    return RegisterPatientResponse(
        user=UserOut(
            id=user.id,
            subject=user.subject,
            role=user.role,
            clinic_id=user.clinic_id,
            patient_id=user.patient_id,
            created_at=user.created_at,
            active_sessions=1,
        ),
        patient_id=patient.id,
        pseudonym=patient.pseudonym,
        episode_id=episode.id,
        tokens=tokens,
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
