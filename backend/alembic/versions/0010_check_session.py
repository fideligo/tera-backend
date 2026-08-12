"""A check session that exists before a measurement does (PM spec sections 28, 30, 31).

``measurement_session`` is a *sensor capture*: it requires per-beat intervals, a device profile and
a quality block, and none of those exist for a BP-only check or for any check before capture. So
PRE-01 and CTX-01 had nothing to attach to — the first was not persisted at all and the second fell
back to an episode-scoped event.

``check_session`` is the spec's own model: created at the start of the flow in **both** modes, with
a mode and a status that walk the section 31 state machine. A sensor capture links back to it.

``session_context`` is re-pointed from ``measurement_session`` to ``check_session``. It was
introduced in 0009 earlier today and holds no rows, so the column is recreated rather than
back-filled; the check for that is in ``upgrade`` and it refuses rather than guessing.

Revision ID: 0010_check_session
Revises: 0009_phr_and_session_context
Create Date: 2026-08-12

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_check_session"
down_revision: str | None = "0009_phr_and_session_context"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_STATUSES = (
    "created",
    "reference_pending",
    "precheck_pending",
    "context_pending",
    "capture_pending",
    "processing",
    "completed",
    "abandoned",
    "failed_quality",
)


def upgrade() -> None:
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'check_session_created'")
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'preconditions_recorded'")

    op.create_table(
        "check_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.Enum("sensor", "bp_only", name="check_mode"), nullable=False),
        sa.Column("status", sa.Enum(*_STATUSES, name="check_session_status"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_check_session_end_after_start",
        ),
        sa.ForeignKeyConstraint(
            ["episode_id"], ["monitoring_episode.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_check_session_episode_id"), "check_session", ["episode_id"], unique=False
    )
    op.create_index(
        op.f("ix_check_session_synthetic"), "check_session", ["synthetic"], unique=False
    )

    # Section 28's `preconditions`. Append-only: PRE-01 describes the patient's state before one
    # measurement, and rewriting it later would rewrite what was true then.
    op.create_table(
        "precondition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("check_session_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rested_5_min", sa.Boolean(), nullable=False),
        sa.Column("recent_activity_30_min", sa.Boolean(), nullable=False),
        sa.Column("recent_caffeine_30_min", sa.Boolean(), nullable=False),
        sa.Column("recent_nicotine_30_min", sa.Boolean(), nullable=False),
        sa.Column("needs_restroom", sa.Boolean(), nullable=False),
        #: Derived on write from the five answers, so "ready" can never disagree with them.
        sa.Column("is_ready", sa.Boolean(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(
            ["check_session_id"], ["check_session.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_precondition_session_recorded",
        "precondition",
        ["check_session_id", "recorded_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_precondition_synthetic"), "precondition", ["synthetic"], unique=False
    )

    # A sensor capture belongs to a check session. Nullable because every session submitted before
    # this migration has none, and inventing one would be a fabricated link.
    op.add_column(
        "measurement_session", sa.Column("check_session_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "fk_measurement_session_check_session",
        "measurement_session",
        "check_session",
        ["check_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_measurement_session_check_session_id"),
        "measurement_session",
        ["check_session_id"],
        unique=False,
    )

    # Re-point session_context. It was created in 0009 and should be empty; if it is not, stop
    # rather than drop somebody's clinical rows.
    existing = op.get_bind().execute(sa.text("SELECT count(*) FROM session_context")).scalar_one()
    if existing:
        raise RuntimeError(
            f"session_context holds {existing} rows and this migration re-points its foreign key. "
            "Migrate them deliberately; this will not drop clinical rows."
        )

    op.execute("DROP TRIGGER IF EXISTS trg_session_context_append_only ON session_context;")
    op.drop_constraint("session_context_session_id_fkey", "session_context", type_="foreignkey")
    op.alter_column("session_context", "session_id", new_column_name="check_session_id")
    op.create_foreign_key(
        "fk_session_context_check_session",
        "session_context",
        "check_session",
        ["check_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE TRIGGER trg_session_context_append_only
        BEFORE UPDATE OR DELETE ON session_context
        FOR EACH ROW EXECUTE FUNCTION tera_append_only_guard();
        """
    )

    op.execute(
        """
        CREATE TRIGGER trg_precondition_append_only
        BEFORE UPDATE OR DELETE ON precondition
        FOR EACH ROW EXECUTE FUNCTION tera_append_only_guard();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_precondition_append_only ON precondition;")
    op.drop_index(op.f("ix_precondition_synthetic"), table_name="precondition")
    op.drop_index("ix_precondition_session_recorded", table_name="precondition")
    op.drop_table("precondition")

    op.execute("DROP TRIGGER IF EXISTS trg_session_context_append_only ON session_context;")
    op.drop_constraint("fk_session_context_check_session", "session_context", type_="foreignkey")
    op.alter_column("session_context", "check_session_id", new_column_name="session_id")
    op.create_foreign_key(
        "session_context_session_id_fkey",
        "session_context",
        "measurement_session",
        ["session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        """
        CREATE TRIGGER trg_session_context_append_only
        BEFORE UPDATE OR DELETE ON session_context
        FOR EACH ROW EXECUTE FUNCTION tera_append_only_guard();
        """
    )

    op.drop_index(
        op.f("ix_measurement_session_check_session_id"), table_name="measurement_session"
    )
    op.drop_constraint(
        "fk_measurement_session_check_session", "measurement_session", type_="foreignkey"
    )
    op.drop_column("measurement_session", "check_session_id")

    op.drop_index(op.f("ix_check_session_synthetic"), table_name="check_session")
    op.drop_index(op.f("ix_check_session_episode_id"), table_name="check_session")
    op.drop_table("check_session")

    for enum_name in ("check_session_status", "check_mode"):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
