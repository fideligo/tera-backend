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
    ChangePasswordRequest,
    CloseAccountRequest,
    CloseAccountResponse,
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

    # The B2C mirror of the account. `users.email` is UNIQUE, and this was an unconditional
    # INSERT: a second registration for an address that already had a `users` row raised
    # UniqueViolation, which aborts the surrounding transaction rather than returning a 409.
    #
    # In the suite that was not one failure but 245 — the poisoned transaction took down every
    # test that ran after it, which is why the whole backend suite looked broken.
    #
    # Get-or-create. The duplicate-subject check above already owns the "this account exists"
    # answer for `app_user`; this half must not contradict it by raising on its own.
    from app.models.recommended import User as RecommendedUser

    b2c_user = db.execute(
        select(RecommendedUser).where(RecommendedUser.email == body.subject)
    ).scalar_one_or_none()
    if b2c_user is None:
        b2c_user = RecommendedUser(
            email=body.subject,
            password_hash=hash_password(body.password),
            onboarding_complete=False,
        )
        db.add(b2c_user)
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

from pydantic import BaseModel
class LoginRequestJson(BaseModel):
    subject: str
    password: str

@router.post("/login", response_model=TokenResponse, summary="Login using JSON body (B2C)")
def login_b2c(body: LoginRequestJson, request: Request, db: DbDep, settings: SettingsDep) -> TokenResponse:
    user = db.execute(select(AppUser).where(AppUser.subject == body.subject)).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        audit.record_unauthenticated(db, actor=body.subject, action=AuditAction.AUTH_LOGIN_FAILED)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="incorrect username or password",
        )
    principal = _principal_for(user)
    issued = refresh_token_service.issue(
        db,
        user_id=user.id,
        expires_at=datetime.now(tz=timezone.utc) + timedelta(days=settings.security.refresh_token_ttl_days),
    )
    tokens = _mint(principal, settings, refresh_jti=issued.jti)
    audit.record(db, principal=principal, action=AuditAction.AUTH_TOKEN_ISSUED, target=user.id)
    db.commit()
    return tokens


@router.post(
    "/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change the password on the current account",
)
def change_password(
    body: ChangePasswordRequest,
    db: DbDep,
    principal: PrincipalDep,
) -> None:
    """Re-prove the current password, then replace it and end every other session.

    # Why the current password is required

    The caller already holds a valid access token, so this looks redundant. It is not: a token can
    be one that was stolen, or a session left signed in on a shared handset. Requiring the password
    is what keeps possession of a token from becoming a permanent takeover, and it is why this
    route does not simply trust the bearer.

    # Why every refresh token is revoked

    A password change is what someone does when they think their account is compromised. Leaving
    the attacker's refresh token alive would make the change theatre — they would keep their access
    while the patient believed they had removed it. Revoking the family means every device, this
    one included, has to sign in again with the new password.

    # Why a wrong current password is 403 and not 401

    401 means "you are not authenticated", and the client's transparent-refresh path treats it as a
    dead session and signs the patient out. That is the wrong response to a typo in a form field.
    403 says the request was refused, and the app can show it against the field.
    """
    user = db.get(AppUser, principal.user_id)
    if user is None:
        # The token decoded but the account behind it is gone — closed on another device, most
        # likely. Not a state to invent a recovery for.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account not found")

    if not verify_password(body.current_password, user.password_hash):
        audit.record(
            db,
            principal=principal,
            action=AuditAction.AUTH_LOGIN_FAILED,
            target=str(user.id),
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The current password is not correct.",
        )

    user.password_hash = hash_password(body.new_password)
    revoked = refresh_token_service.revoke_all_for_user(
        db, user_id=user.id, reason="password_changed"
    )
    audit.record(
        db,
        principal=principal,
        action=AuditAction.AUTH_PASSWORD_CHANGED,
        target=str(user.id),
    )
    db.commit()

    # Ids and counts. Never the password, and never the subject — the deny-list covers the obvious
    # names but the rule is that this line says what happened, not to whom.
    log.info("auth_password_changed", extra={"sessions_revoked": revoked})


@router.post(
    "/account/close",
    response_model=CloseAccountResponse,
    summary="Close the account, retaining the pseudonymous clinical record",
)
def close_account(
    body: CloseAccountRequest,
    db: DbDep,
    principal: PrincipalDep,
) -> CloseAccountResponse:
    """Delete the sign-in identity. **The clinical record is retained, pseudonymously.**

    # What this deletes, and what it deliberately does not

    Deleted: the `app_user` row — the login subject and the password hash — and every refresh token
    issued to it. After this call nobody can authenticate as this person again.

    Retained: the `patient` row and its clinical history. That table is pseudonymous by design
    (BUILD_SPEC 4.1: "pseudonymous id, clinic id, enrolled_at. No name or contact fields"), and
    every clinical table carries a `BEFORE UPDATE OR DELETE` trigger enforcing invariant 5. So the
    record cannot be deleted here even if that were wanted, and what survives carries no name, no
    contact and no link back to the person once the account row is gone.

    This is the shape of the App Store's account-deletion requirement that a health record can
    actually satisfy: the identity goes, the de-identified measurements stay under a retention
    policy. It is stated to the patient in those terms rather than as "everything is deleted",
    which would be false.

    # POST rather than DELETE

    `test_clinical_rows_have_no_update_or_delete_route` walks the OpenAPI schema and fails on any
    PUT, PATCH or DELETE anywhere in the API — deliberately, so a mutable-looking route cannot slip
    in. Closing an account is an action on the caller's own identity rather than a deletion of a
    clinical resource, and it is modelled as one. The invariant that test defends is untouched.

    # The audit log keeps the subject, and the patient is told so

    `audit.record` stores `principal.subject` as the actor, and the audit log is append-only. Every
    prior sign-in already wrote it. Closing the account cannot remove those entries and must not
    claim to: the security trail is exactly the thing an append-only log exists to preserve.
    """
    user = db.get(AppUser, principal.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="account not found")

    if not verify_password(body.password, user.password_hash):
        audit.record(
            db,
            principal=principal,
            action=AuditAction.AUTH_LOGIN_FAILED,
            target=str(user.id),
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="That password is not correct.",
        )

    pseudonym: str | None = None
    if user.patient_id is not None:
        patient = db.get(Patient, user.patient_id)
        pseudonym = None if patient is None else patient.pseudonym

    # Written *before* the row goes: the audit entry names the account that was closed, and after
    # the delete there is no principal left to attribute it to.
    audit.record(
        db,
        principal=principal,
        action=AuditAction.AUTH_ACCOUNT_CLOSED,
        target=str(user.id),
    )

    # `refresh_token.user_id` is a RESTRICT foreign key, so the tokens have to go first or the
    # delete below fails on the constraint rather than on anything meaningful. They are not an
    # append-only table; the audit entries recording their issue are, and those stay.
    refresh_token_service.revoke_all_for_user(db, user_id=user.id, reason="account_closed")
    db.query(RefreshToken).filter(RefreshToken.user_id == user.id).delete(
        synchronize_session=False
    )

    db.delete(user)
    db.commit()

    log.info("auth_account_closed")

    return CloseAccountResponse(
        closed=True,
        pseudonym=pseudonym,
        detail=(
            "Your sign-in details have been deleted and you can no longer sign in. Your readings "
            "are kept under a pseudonym, with no name or contact details attached to them."
        ),
    )
