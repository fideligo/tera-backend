"""Decouple a patient from a clinic, for B2C self-registration.

The product is now a standalone consumer app: nobody enrols the patient, and there is no clinic
behind the account. ``patient.clinic_id`` was NOT NULL, which made a clinic identifier a
precondition for a patient record existing at all.

``monitoring_episode.reviewing_clinician_id`` was already nullable in 0001 and needs no change —
the column was introduced as optional precisely so an episode could exist before a clinician was
assigned to it.

The index on ``patient.clinic_id`` stays. A B2C row simply has no value in it, and a later B2B2C
deployment would want the index back rather than a second migration to recreate it.

Revision ID: 0007_b2c_self_registration
Revises: 0006_rate_limit_counter
Create Date: 2026-08-09

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_b2c_self_registration"
down_revision: str | None = "0006_rate_limit_counter"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "patient",
        "clinic_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )


def downgrade() -> None:
    """Not reversible in general, and deliberately fails loudly rather than guessing.

    Restoring NOT NULL requires a clinic identifier for every self-registered patient, and there
    is no value that would be true. Inventing a placeholder would write a clinic affiliation that
    does not exist into clinical records.
    """
    orphans = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM patient WHERE clinic_id IS NULL"))
        .scalar_one()
    )
    if orphans:
        raise RuntimeError(
            f"{orphans} patient rows have no clinic_id. Assign one deliberately before "
            "downgrading; this migration will not invent an affiliation."
        )
    op.alter_column(
        "patient",
        "clinic_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
