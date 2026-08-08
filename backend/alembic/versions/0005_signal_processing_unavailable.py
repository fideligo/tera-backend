"""A rejection reason for 'the signal chain does not exist in this build'.

The patient app captures both streams correctly but does not yet derive per-beat intervals. Those
sessions must still reach the backend and still be stored (invariant 3) — but recording them as
``poor_signal_quality`` would put a false statement in a clinical record: the signal was not
assessed at all. A distinct value keeps "the signal was bad" separable from "this part of the
system is not built yet", in the database rather than in someone's memory.

Revision ID: 0005_signal_processing_unavailable
Revises: 0004_audit_unauthenticated_actor
Create Date: 2026-08-08

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_signal_processing_unavailable"
down_revision: str | None = "0004_audit_unauthenticated_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Additive only. PostgreSQL 12+ allows ADD VALUE inside a transaction as long as the value is
    # not used in the same transaction; this migration only declares it.
    op.execute(
        "ALTER TYPE rejection_reason ADD VALUE IF NOT EXISTS 'signal_processing_unavailable'"
    )


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value. Reversing this would mean recreating rejection_reason
    # and rewriting every clinical row that uses it — on append-only tables that is a worse
    # outcome than one unused label. Deliberately a no-op, as in 0003.
    pass
