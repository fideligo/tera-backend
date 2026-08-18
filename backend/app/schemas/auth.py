"""Token issue and refresh (BUILD_SPEC 4.5).

DEVIATION: the endpoint table in BUILD_SPEC 4.2 does not list an auth endpoint, but 4.5 mandates
"OAuth2 with short-lived JWT access tokens plus refresh". Tokens have to come from somewhere.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, model_validator

from app.models.enums import UserRole
from app.schemas.common import TeraModel
from app.security.passwords import MAX_PASSWORD_BYTES

#: Long enough to resist guessing, short enough to be typeable. bcrypt caps the input at 72
#: bytes and the schema refuses anything longer rather than silently truncating it, so a user's
#: password always means what they typed.
MIN_PASSWORD_LENGTH = 12


class TokenResponse(TeraModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds.")
    role: UserRole


class RefreshRequest(TeraModel):
    refresh_token: str = Field(max_length=4096)


class LogoutRequest(TeraModel):
    """Ending a session means revoking its refresh token; the access token expires on its own."""

    refresh_token: str | None = Field(
        default=None,
        max_length=4096,
        description="The session to end. Omit it and pass all_sessions to end every session.",
    )
    all_sessions: bool = Field(
        default=False,
        description="Revoke every refresh token this user holds, on every device.",
    )

    @model_validator(mode="after")
    def _something_to_revoke(self) -> "LogoutRequest":
        if not self.refresh_token and not self.all_sessions:
            raise ValueError("provide refresh_token, or set all_sessions to end every session")
        return self


class PasswordGrantRequest(TeraModel):
    """Used by the CLIs; the HTTP endpoint accepts the OAuth2 form encoding as well."""

    username: str = Field(max_length=128)
    password: str = Field(max_length=MAX_PASSWORD_BYTES)


class RegisterRequest(TeraModel):
    """Create an account. Admin-only.

    IMPLEMENTATION DETAIL, not a proposal requirement. The proposal describes enrolment as
    clinic-initiated — a patient is enrolled into a monitoring episode by a clinic when
    treatment is adjusted — so there is no self-registration path to build. Restricting this
    to admins is how that is enforced.
    """

    #: Plain constrained string rather than ``EmailStr``: that would pull in `email-validator`,
    #: and a new dependency needs a better reason than format-checking a login identifier the
    #: admin typed themselves.
    subject: str = Field(min_length=3, max_length=128, description="Login identifier.")
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_BYTES)
    role: UserRole
    clinic_id: str | None = Field(default=None, max_length=64)
    #: Required for a patient account and forbidden otherwise; the database CHECK enforces the
    #: same rule, so the two cannot drift.
    patient_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _patient_link_matches_role(self) -> "RegisterRequest":
        if self.role is UserRole.PATIENT and self.patient_id is None:
            raise ValueError("a patient account must name the patient record it belongs to")
        if self.role is not UserRole.PATIENT and self.patient_id is not None:
            raise ValueError("only a patient account may name a patient record")
        return self


class UserOut(TeraModel):
    """Who the caller is. Deliberately thin — an identity endpoint is not a data endpoint."""

    id: uuid.UUID
    subject: str
    role: UserRole
    clinic_id: str | None
    patient_id: uuid.UUID | None
    created_at: datetime
    active_sessions: int = Field(
        description="Refresh tokens this account could still present, across all devices."
    )


class RegisterPatientRequest(TeraModel):
    """Self-registration for the standalone consumer app.

    B2C PIVOT. `RegisterRequest` above is admin-only because enrolment used to be clinic-initiated
    — a clinic opened an episode when it adjusted someone's treatment. In a standalone app there
    is no clinic to do that, so the account, the patient record and the first monitoring episode
    are all created by the person signing up.

    There is deliberately no `role` field: this endpoint mints patients and nothing else. A role
    parameter on a public route is a privilege-escalation surface, and defaulting it would be one
    typo away from a self-service admin account.
    """

    subject: str = Field(min_length=3, max_length=128, description="Login identifier.")
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_BYTES)


class RegisterPatientResponse(TeraModel):
    """The new account, its patient record, and the episode opened for it.

    Returns tokens as well, so signing up does not immediately require a second round trip to a
    login form the user has just filled in.
    """

    user: UserOut
    patient_id: uuid.UUID
    pseudonym: str
    episode_id: uuid.UUID
    tokens: TokenResponse


class ChangePasswordRequest(TeraModel):
    """`POST /v1/auth/password`.

    The current password is required even though the caller already holds a valid access token.
    A token can be a stolen one, or a session left open on a shared handset; re-proving the
    password is what stops either of those becoming a permanent takeover, and it is the reason
    this endpoint cannot simply trust the bearer.
    """

    current_password: str = Field(max_length=MAX_PASSWORD_BYTES)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_BYTES)


class CloseAccountRequest(TeraModel):
    """`POST /v1/auth/account/close`.

    Password-confirmed for the same reason as above, and more so: this one cannot be undone.
    """

    password: str = Field(max_length=MAX_PASSWORD_BYTES)


class CloseAccountResponse(TeraModel):
    """What closing the account actually did.

    Returned rather than a bare 204 because the two halves are not symmetrical and the client has
    to be able to say so. The sign-in credential is gone; the clinical record is not, and telling
    a patient otherwise would be a false claim about their own data.
    """

    closed: bool
    #: The pseudonym the retained clinical record now stands under, with nothing linking it back.
    #: Returned once, here, because after this call there is no authenticated way to ask for it.
    pseudonym: str | None = None
    detail: str
