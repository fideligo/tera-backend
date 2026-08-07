"""Token issue and refresh (BUILD_SPEC 4.5).

DEVIATION: the endpoint table in BUILD_SPEC 4.2 does not list an auth endpoint, but 4.5 mandates
"OAuth2 with short-lived JWT access tokens plus refresh". Tokens have to come from somewhere.
"""

from __future__ import annotations

from pydantic import Field

from app.models.enums import UserRole
from app.schemas.common import TeraModel
from app.security.passwords import MAX_PASSWORD_BYTES


class TokenResponse(TeraModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access-token lifetime in seconds.")
    role: UserRole


class RefreshRequest(TeraModel):
    refresh_token: str = Field(max_length=4096)


class PasswordGrantRequest(TeraModel):
    """Used by the CLIs; the HTTP endpoint accepts the OAuth2 form encoding as well."""

    username: str = Field(max_length=128)
    password: str = Field(max_length=MAX_PASSWORD_BYTES)
