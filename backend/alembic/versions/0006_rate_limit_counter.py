"""Cross-process rate-limit counters for the auth endpoints.

The in-memory limiter counts per worker, so with N workers the effective ceiling is N times the
configured one. On the ingest endpoints that costs some extra database work. On the auth endpoints
the ceiling *is* the brute-force defence, and one that silently multiplies by the worker count is
a number in a config file rather than a control.

Postgres rather than Redis, following the session_nonce precedent: one shared counter is all this
needs, Postgres is already here, and Redis would be a new operational dependency for one table.

Not a clinical table — no clinical content, and counting requires UPDATE, which the append-only
trigger would forbid.

Revision ID: 0006_rate_limit_counter
Revises: 0005_signal_proc_unavailable
Create Date: 2026-08-09

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_rate_limit_counter"
down_revision: str | None = "0005_signal_proc_unavailable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_counter",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("bucket", sa.String(64), nullable=False),
        # Hashed by the caller: an attempted username is credential-adjacent and a client address
        # is personal data, and neither belongs in a table that only answers "how many".
        sa.Column("subject_key", sa.String(128), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        # The constraint is the correctness mechanism, not a tidiness measure: the increment is a
        # single INSERT ... ON CONFLICT DO UPDATE, so two workers cannot both read a count below
        # the limit and both allow the request.
        sa.UniqueConstraint(
            "bucket",
            "subject_key",
            "window_start",
            name="uq_rate_limit_counter_bucket_subject_window",
        ),
    )
    op.create_index(
        "ix_rate_limit_counter_window_start", "rate_limit_counter", ["window_start"]
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_counter_window_start", table_name="rate_limit_counter")
    op.drop_table("rate_limit_counter")
