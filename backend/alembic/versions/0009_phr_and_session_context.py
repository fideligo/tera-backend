"""PHR profile and per-session context, from PM spec sections 28 and 30.

Two tables, and they are deliberately unlike each other.

``phr_profile`` is **mutable**. It describes a person as they are now — a weight, a hypertension
status — and a PATCH is the whole point of it. It is therefore *not* a clinical record in
invariant 5's sense and gets no append-only trigger: a corrected height is a correction, not a new
fact about a different moment.

``session_context`` is **append-only**, like every other clinical table. It describes one check at
one moment: what the patient reported around *that* measurement. Editing it later would rewrite
what was true then, which is exactly what invariant 5 exists to prevent. The spec's own
`PATCH .../context` is therefore implemented as an insert-and-supersede, not an update.

Revision ID: 0009_phr_and_session_context
Revises: 0008_patient_context
Create Date: 2026-08-12

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_phr_and_session_context"
down_revision: str | None = "0008_patient_context"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'phr_profile_updated'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'session_context_recorded'")

    op.create_table(
        "phr_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        # One row per patient. Mutable, so the patient id is unique rather than a history key.
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column(
            "sex_assigned_at_birth",
            sa.Enum("female", "male", "prefer_not_to_say", name="sex_at_birth"),
            nullable=True,
        ),
        sa.Column("height_cm", sa.Float(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=True),
        sa.Column(
            "hypertension_status",
            sa.Enum("diagnosed", "not_diagnosed", "not_sure", name="hypertension_status"),
            nullable=True,
        ),
        sa.Column("taking_bp_medication", sa.Boolean(), nullable=True),
        #: Section 28's `health_conditions`, folded in as an array. A separate table buys
        #: per-condition timestamps nobody collects yet, and an array is one migration to unfold.
        sa.Column(
            "conditions",
            postgresql.ARRAY(sa.String(length=64)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Sanity bounds, not clinical thresholds: they catch a slipped decimal point. A value
        # inside them is not a judgement about anybody, and no BMI is derived anywhere.
        sa.CheckConstraint(
            "height_cm IS NULL OR (height_cm >= 50 AND height_cm <= 250)",
            name="ck_phr_profile_height_plausible",
        ),
        sa.CheckConstraint(
            "weight_kg IS NULL OR (weight_kg >= 10 AND weight_kg <= 400)",
            name="ck_phr_profile_weight_plausible",
        ),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("patient_id", name="uq_phr_profile_patient"),
    )
    op.create_index(op.f("ix_phr_profile_synthetic"), "phr_profile", ["synthetic"], unique=False)

    op.create_table(
        "session_context",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sleep_less_than_usual", sa.Boolean(), nullable=False),
        sa.Column("stress_higher_than_usual", sa.Boolean(), nullable=False),
        sa.Column("feeling_unwell", sa.Boolean(), nullable=False),
        #: The spec's `symptoms_json`. Bounded by the request schema, as the event payload is.
        sa.Column(
            "symptoms",
            postgresql.ARRAY(sa.String(length=64)),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column(
            "medication_status_today",
            sa.Enum(
                "as_usual",
                "missed_or_late",
                "not_applicable",
                "not_sure",
                name="medication_status_today",
            ),
            nullable=False,
        ),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(
            ["session_id"], ["measurement_session.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_session_context_session_id"), "session_context", ["session_id"], unique=False
    )
    op.create_index(
        op.f("ix_session_context_synthetic"), "session_context", ["synthetic"], unique=False
    )
    # Latest-in-force lookups are the only read pattern.
    op.create_index(
        "ix_session_context_session_recorded",
        "session_context",
        ["session_id", "recorded_at"],
        unique=False,
    )

    # Invariant 5, via the function installed in 0001. session_context only: phr_profile is
    # deliberately mutable, and the reasoning is in this migration's docstring.
    op.execute(
        """
        CREATE TRIGGER trg_session_context_append_only
        BEFORE UPDATE OR DELETE ON session_context
        FOR EACH ROW EXECUTE FUNCTION tera_append_only_guard();
        """
    )


def downgrade() -> None:
    # The audit_action values stay: Postgres cannot drop an enum value without rewriting the type,
    # and a documented no-op downgrade follows 0003's precedent.
    op.execute("DROP TRIGGER IF EXISTS trg_session_context_append_only ON session_context;")
    op.drop_index("ix_session_context_session_recorded", table_name="session_context")
    op.drop_index(op.f("ix_session_context_synthetic"), table_name="session_context")
    op.drop_index(op.f("ix_session_context_session_id"), table_name="session_context")
    op.drop_table("session_context")

    op.drop_index(op.f("ix_phr_profile_synthetic"), table_name="phr_profile")
    op.drop_table("phr_profile")

    for enum_name in ("medication_status_today", "hypertension_status", "sex_at_birth"):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
