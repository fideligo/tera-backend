"""JWT access and refresh tokens (BUILD_SPEC 4.5)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import jwt

from app.config import SecuritySettings
from app.models.enums import UserRole

TokenType = Literal["access", "refresh"]


class TokenError(Exception):
    """Raised when a token is absent, malformed, expired or of the wrong type."""


@dataclass(frozen=True)
class Principal:
    """The authenticated caller.

    ``patient_id`` is set only for patient tokens and is the sole basis for patient-scoped
    access — a patient token cannot reach another patient's data by naming their id in a path.
    """

    subject: str
    role: UserRole
    user_id: uuid.UUID
    patient_id: uuid.UUID | None = None
    clinic_id: str | None = None

    #: The token's ``jti``. For a refresh token this is the key into ``refresh_token``, which is
    #: what makes revocation possible at all — a JWT on its own cannot be taken back.
    jti: str | None = None

    @property
    def is_patient(self) -> bool:
        return self.role is UserRole.PATIENT

    @property
    def is_clinician(self) -> bool:
        return self.role is UserRole.CLINICIAN

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN


def issue_token(
    *,
    principal: Principal,
    token_type: TokenType,
    settings: SecuritySettings,
    now: datetime | None = None,
    jti: str | None = None,
) -> tuple[str, datetime]:
    """Mint a signed token and return it with its expiry.

    ``jti`` is supplied by the caller for refresh tokens, so the claim matches the
    ``refresh_token`` row that records it. Access tokens get a fresh one and are not tracked
    server-side: their fifteen-minute lifetime is the whole revocation strategy.
    """
    issued_at = now or datetime.now(tz=timezone.utc)
    lifetime = (
        timedelta(minutes=settings.access_token_ttl_minutes)
        if token_type == "access"
        else timedelta(days=settings.refresh_token_ttl_days)
    )
    expires_at = issued_at + lifetime

    claims: dict[str, Any] = {
        "sub": principal.subject,
        "uid": str(principal.user_id),
        "role": principal.role.value,
        "typ": token_type,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": jti or uuid.uuid4().hex,
    }
    if principal.patient_id is not None:
        claims["pid"] = str(principal.patient_id)
    if principal.clinic_id is not None:
        claims["cid"] = principal.clinic_id

    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_token(
    token: str, *, expected_type: TokenType, settings: SecuritySettings
) -> Principal:
    """Verify a token and return the principal it names.

    A refresh token presented as an access token is rejected: the ``typ`` claim is checked, not
    just the signature. Otherwise a long-lived refresh token would work as a long-lived access
    token, defeating the point of short access-token TTLs.
    """
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("token is invalid") from exc

    if claims.get("typ") != expected_type:
        raise TokenError(f"expected a {expected_type} token")

    try:
        role = UserRole(claims["role"])
        user_id = uuid.UUID(claims["uid"])
        subject = claims["sub"]
    except (KeyError, ValueError) as exc:
        raise TokenError("token is missing required claims") from exc

    patient_id = uuid.UUID(claims["pid"]) if claims.get("pid") else None
    return Principal(
        subject=subject,
        role=role,
        user_id=user_id,
        patient_id=patient_id,
        clinic_id=claims.get("cid"),
        jti=claims.get("jti"),
    )
