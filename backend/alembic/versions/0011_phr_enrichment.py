"""Medications, family history, lifestyle and women's health (PM spec sections 8, 25, 28).

Onboarding collects the minimum the safety gates need; this is the progressive enrichment that
happens later in Profile.

**Two shapes, for two reasons.** Family history, lifestyle and the women's-health answer are all
one value per patient, so they are columns on `phr_profile` — the mutable profile table that
already exists. Medications are a *list* that changes over time, so they get their own table.
Section 28 also models lifestyle and family history as tables; folding the single-valued answers
into the profile is a deviation recorded in `docs/decisions.md`, taken because a table per answer
buys per-field timestamps nobody collects.

`medication` is **not append-only**. A medication list describes what a patient is taking now, and
correcting a mistyped dose is a correction rather than a new fact about a different moment — the
same reasoning that keeps `phr_profile` mutable. `last_changed_at` carries the clinically
interesting date, and PROF-04's reference refresh is driven off it.

Revision ID: 0011_phr_enrichment
Revises: 0010_check_session
Create Date: 2026-08-12

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_phr_enrichment"
down_revision: str | None = "0010_check_session"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ENUMS = {
    "family_bp_history": ("parent", "sibling", "both", "no", "not_sure"),
    "physical_activity_level": ("rarely", "one_to_two_days", "three_to_four_days", "five_plus_days"),
    "smoking_status": ("never", "formerly", "currently"),
    "usual_sleep_hours": ("under_5", "five_to_six", "seven_to_eight", "nine_plus"),
    "usual_stress_level": ("low", "moderate", "high"),
    "alcohol_frequency": ("never", "occasionally", "weekly", "daily"),
    "pregnancy_hypertension_history": ("yes", "no", "not_sure", "not_applicable"),
    "medication_status": ("active", "stopped"),
}


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'medications_updated'")

    for column, enum_values in (
        ("family_bp_history", "family_bp_history"),
        ("physical_activity", "physical_activity_level"),
        ("smoking_status", "smoking_status"),
        ("usual_sleep_hours", "usual_sleep_hours"),
        ("usual_stress_level", "usual_stress_level"),
        ("alcohol_frequency", "alcohol_frequency"),
        ("pregnancy_hypertension_history", "pregnancy_hypertension_history"),
    ):
        enum_type = sa.Enum(*_ENUMS[enum_values], name=enum_values)
        enum_type.create(op.get_bind(), checkfirst=True)
        op.add_column(
            "phr_profile",
            sa.Column(column, enum_type, nullable=True),
        )

    # The optional second family question, separate from the BP one because "no BP history" and
    # "no early heart disease" are different answers to different questions.
    op.add_column(
        "phr_profile", sa.Column("family_early_cardiac_history", sa.Boolean(), nullable=True)
    )

    op.create_table(
        "medication",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("dose", sa.String(length=64), nullable=False),
        sa.Column("frequency", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.Date(), nullable=True),
        #: PROF-04's trigger: a change here is what sets the BP reference to needing a refresh.
        sa.Column("last_changed_at", sa.Date(), nullable=True),
        sa.Column(
            "status", sa.Enum(*_ENUMS["medication_status"], name="medication_status", create_type=True), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_medication_patient_id"), "medication", ["patient_id"], unique=False)
    op.create_index(op.f("ix_medication_synthetic"), "medication", ["synthetic"], unique=False)

    # PROF-04: "If BP medication changes: force_refresh_bp_reference = true". Until now nothing
    # ever set this — the flag was read by needsBPReference and written by nobody.
    op.add_column(
        "monitoring_episode",
        sa.Column(
            "force_reference_refresh",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("monitoring_episode", "force_reference_refresh")

    op.drop_index(op.f("ix_medication_synthetic"), table_name="medication")
    op.drop_index(op.f("ix_medication_patient_id"), table_name="medication")
    op.drop_table("medication")

    for column in (
        "family_early_cardiac_history",
        "pregnancy_hypertension_history",
        "alcohol_frequency",
        "usual_stress_level",
        "usual_sleep_hours",
        "smoking_status",
        "physical_activity",
        "family_bp_history",
    ):
        op.drop_column("phr_profile", column)

    for enum_name in _ENUMS:
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
