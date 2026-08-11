"""Patient-supplied clinical context, append-only.

B2C PIVOT. With no clinic behind the account, the intake form is the only source of medication,
pregnancy and rhythm history — and the only place a contraindication can be caught. It was
handset-only, which meant it was lost on uninstall and invisible to the server.

**Append-only, latest-wins on read.** A patient who changes an answer does not erase the previous
one: what they said in June is a fact about June, and the deviation engine reads a session against
the context in force when it was captured. Same reasoning as ``calibration``, and the same
enforcement — the shared ``tera_append_only_guard`` trigger.

``last_clinic_bp`` is three columns rather than JSONB so a CHECK can hold them together: all three
present or all three absent. A systolic with no date is not a reading.

Revision ID: 0008_patient_context
Revises: 0007_b2c_self_registration
Create Date: 2026-08-09

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_patient_context"
down_revision: str | None = "0007_b2c_self_registration"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

TABLE = "patient_context"


def upgrade() -> None:
    # Declared before the table so the value exists by the time anything writes it. PostgreSQL 12+
    # permits ADD VALUE inside a transaction provided the value is not *used* in the same one.
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'patient_context_recorded'")

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_regimen_change_date", sa.DateTime(timezone=True), nullable=True),
        # One object per medicine: {"name": ..., "dose": ...}. Bounded by the schema, not here.
        sa.Column(
            "medications",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "pregnant",
            sa.Enum("yes", "no", "prefer_not_to_say", name="pregnancy_answer"),
            nullable=False,
        ),
        sa.Column("known_arrhythmia", sa.Boolean(), nullable=False),
        sa.Column("last_clinic_systolic_mmhg", sa.Integer(), nullable=True),
        sa.Column("last_clinic_diastolic_mmhg", sa.Integer(), nullable=True),
        sa.Column("last_clinic_taken_on", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "(last_clinic_systolic_mmhg IS NULL) = (last_clinic_diastolic_mmhg IS NULL) AND "
            "(last_clinic_systolic_mmhg IS NULL) = (last_clinic_taken_on IS NULL)",
            name="ck_patient_context_clinic_bp_all_or_nothing",
        ),
        sa.CheckConstraint(
            "last_clinic_systolic_mmhg IS NULL OR "
            "last_clinic_systolic_mmhg > last_clinic_diastolic_mmhg",
            name="ck_patient_context_clinic_bp_ordered",
        ),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f(f"ix_{TABLE}_patient_id"), TABLE, ["patient_id"], unique=False)
    op.create_index(op.f(f"ix_{TABLE}_synthetic"), TABLE, ["synthetic"], unique=False)
    # Latest-in-force lookups are the only read pattern.
    op.create_index(
        f"ix_{TABLE}_patient_recorded", TABLE, ["patient_id", "recorded_at"], unique=False
    )

    # Invariant 5, via the function installed in 0001.
    op.execute(
        f"""
        CREATE TRIGGER trg_{TABLE}_append_only
        BEFORE UPDATE OR DELETE ON {TABLE}
        FOR EACH ROW EXECUTE FUNCTION tera_append_only_guard();
        """
    )


def downgrade() -> None:
    # The audit_action value is left in place: Postgres cannot drop an enum value without
    # rewriting the type, and a documented no-op downgrade follows 0003's precedent.
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_append_only ON {TABLE};")
    op.drop_index(f"ix_{TABLE}_patient_recorded", table_name=TABLE)
    op.drop_index(op.f(f"ix_{TABLE}_synthetic"), table_name=TABLE)
    op.drop_index(op.f(f"ix_{TABLE}_patient_id"), table_name=TABLE)
    op.drop_table(TABLE)
    sa.Enum(name="pregnancy_answer").drop(op.get_bind(), checkfirst=True)
