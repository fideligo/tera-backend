"""Shared response pieces."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.services import language


class TeraModel(BaseModel):
    """Base for every request and response model."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class SyntheticFlag(BaseModel):
    """Invariant 9 — synthetic data is unmistakably labelled in the API.

    Mixed into every response that carries a stored row. ``synthetic_notice`` is populated only
    when the row is synthetic, so a client that renders it unconditionally shows nothing for
    real data and an unmissable notice for seeded data.
    """

    synthetic: bool = Field(
        description="True when this record was generated for demonstration and is not a "
        "measurement from a person."
    )
    synthetic_notice: str | None = Field(
        default=None,
        description=f"Set to '{language.SYNTHETIC_BADGE}' when synthetic is true.",
    )

    @classmethod
    def notice_for(cls, synthetic: bool) -> str | None:
        return language.SYNTHETIC_BADGE if synthetic else None


class ViolationDetail(TeraModel):
    """One validation failure, returned in a 422 body."""

    field: str
    message: str


class ErrorResponse(TeraModel):
    """Uniform error envelope."""

    detail: str
    violations: list[ViolationDetail] = Field(default_factory=list)
