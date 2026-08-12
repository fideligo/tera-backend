"""The BP reference lifecycle (PM spec sections 12, 27, 28's ``bp_references``).

The last table in section 28 with no home. Until now "which cuff reading is this patient's
baseline" lived only on the handset, in `AppFlowState.reference` — so it did not survive a
reinstall, could not be read by the rule engine, and could not answer "which reference was in
force when this check ran".

**A pointer, not a copy.** The mmHg stay in `cuff_reading` (invariant 1) and this row names which
reading is in force. Copying systolic and diastolic here would create a second table holding
pressure values and a way for the two to disagree.

**Supersession mirrors `calibration` exactly**, including the partial unique index and the
one-way trigger: at most one `active` row per patient, and replacing a reference inserts a new row
and stamps `deactivated_at` on the old one. The table is deliberately *not* in `CLINICAL_TABLES` —
like `calibration`, its supersession columns are the one sanctioned mutation, and the append-only
trigger would forbid them.

Revision ID: 0012_bp_reference
Revises: 0011_phr_enrichment
Create Date: 2026-08-12

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_bp_reference"
down_revision: str | None = "0011_phr_enrichment"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_STATUS = ("active", "superseded")
_REFRESH_REASON = (
    "first_reference",
    "monitoring_gap",
    "medication_change",
    "persistent_trend",
    "manual_refresh",
    "health_change",
)


def upgrade() -> None:
    # ADD VALUE cannot run inside the transaction that later uses the value, so these are emitted
    # first and committed by Alembic's own transaction boundary before any INSERT needs them.
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'bp_reference_activated'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'check_session_advanced'")

    # Created explicitly, then referenced with ``create_type=False``. Without that flag
    # ``create_table`` emits its own CREATE TYPE for the same name and the migration dies on the
    # duplicate — the enum has already been made two lines above.
    sa.Enum(*_STATUS, name="bp_reference_status").create(op.get_bind(), checkfirst=True)
    sa.Enum(*_REFRESH_REASON, name="bp_reference_refresh_reason").create(
        op.get_bind(), checkfirst=True
    )
    status = postgresql.ENUM(*_STATUS, name="bp_reference_status", create_type=False)
    reason = postgresql.ENUM(
        *_REFRESH_REASON, name="bp_reference_refresh_reason", create_type=False
    )

    op.create_table(
        "bp_reference",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("cuff_reading_id", sa.Uuid(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_reason", reason, nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["cuff_reading_id"], ["cuff_reading.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # The two halves of "active" can never disagree: a row is active exactly when it has not
        # been deactivated. Without this the status could say active while a deactivation
        # timestamp sat beside it.
        sa.CheckConstraint(
            "(status = 'active') = (deactivated_at IS NULL)",
            name="ck_bp_reference_active_iff_not_deactivated",
        ),
        sa.CheckConstraint(
            "deactivated_at IS NULL OR deactivated_at >= activated_at",
            name="ck_bp_reference_deactivated_after_activated",
        ),
    )
    op.create_index(
        op.f("ix_bp_reference_patient_id"), "bp_reference", ["patient_id"], unique=False
    )
    op.create_index(
        op.f("ix_bp_reference_synthetic"), "bp_reference", ["synthetic"], unique=False
    )

    # At most one active reference per patient. The same partial-index device as
    # `uq_calibration_one_active_per_patient_device`, and load-bearing for the same reason: two
    # active baselines means every trend is computed against an arbitrary one of them.
    op.create_index(
        "uq_bp_reference_one_active_per_patient",
        "bp_reference",
        ["patient_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # Supersession is one-way, and it is the only mutation allowed. An active reference may be
    # retired; a retired one may never come back, and the reading it points at never changes.
    # Same trigger shape as `calibration`, for the same argument recorded in CLAUDE.md section 5.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bp_reference_supersede_only()
        RETURNS TRIGGER AS $$
        BEGIN
            IF OLD.status = 'superseded' THEN
                RAISE EXCEPTION 'bp_reference % is superseded and cannot be modified', OLD.id;
            END IF;
            IF NEW.patient_id <> OLD.patient_id
               OR NEW.cuff_reading_id <> OLD.cuff_reading_id
               OR NEW.activated_at <> OLD.activated_at
               OR NEW.refresh_reason <> OLD.refresh_reason THEN
                RAISE EXCEPTION 'bp_reference % may only be superseded, not rewritten', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bp_reference_supersede_only
        BEFORE UPDATE ON bp_reference
        FOR EACH ROW EXECUTE FUNCTION bp_reference_supersede_only();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION bp_reference_no_delete()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'bp_reference rows are never deleted';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_bp_reference_no_delete
        BEFORE DELETE ON bp_reference
        FOR EACH ROW EXECUTE FUNCTION bp_reference_no_delete();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_bp_reference_no_delete ON bp_reference")
    op.execute("DROP FUNCTION IF EXISTS bp_reference_no_delete()")
    op.execute("DROP TRIGGER IF EXISTS trg_bp_reference_supersede_only ON bp_reference")
    op.execute("DROP FUNCTION IF EXISTS bp_reference_supersede_only()")

    op.drop_index("uq_bp_reference_one_active_per_patient", table_name="bp_reference")
    op.drop_index(op.f("ix_bp_reference_synthetic"), table_name="bp_reference")
    op.drop_index(op.f("ix_bp_reference_patient_id"), table_name="bp_reference")
    op.drop_table("bp_reference")

    sa.Enum(name="bp_reference_refresh_reason").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="bp_reference_status").drop(op.get_bind(), checkfirst=True)
