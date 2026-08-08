"""Refresh-token records, for rotation and revocation.

A JWT cannot be taken back once issued. For a 15-minute access token that is acceptable; for a
14-day refresh token it means a leaked token is a fortnight of access to a health record with no
way to end it. This table makes revocation and rotation possible.

Deliberately NOT append-only: revocation is an update, and the trigger on the clinical tables
would forbid it. The table holds no clinical content.

Revision ID: 0002_refresh_token_rotation
Revises: 0001_initial_schema
Create Date: 2026-08-07

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_refresh_token_rotation"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=64), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        # Groups every token descended from one login, so a compromised chain can be revoked
        # without walking the replaced_by links.
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_reason IS NOT NULL",
            name="ck_refresh_token_revoked_has_reason",
        ),
        sa.CheckConstraint(
            "(superseded_at IS NULL) = (replaced_by_id IS NULL)",
            name="ck_refresh_token_superseded_has_successor",
        ),
        sa.CheckConstraint(
            "replaced_by_id IS NULL OR replaced_by_id <> id",
            name="ck_refresh_token_not_self_replacing",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["replaced_by_id"], ["refresh_token.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_refresh_token_jti"), "refresh_token", ["jti"], unique=True)
    op.create_index(op.f("ix_refresh_token_user_id"), "refresh_token", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_refresh_token_family_id"), "refresh_token", ["family_id"], unique=False
    )
    op.create_index(
        "ix_refresh_token_active",
        "refresh_token",
        ["user_id", "revoked_at", "superseded_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_refresh_token_active", table_name="refresh_token")
    op.drop_index(op.f("ix_refresh_token_family_id"), table_name="refresh_token")
    op.drop_index(op.f("ix_refresh_token_user_id"), table_name="refresh_token")
    op.drop_index(op.f("ix_refresh_token_jti"), table_name="refresh_token")
    op.drop_table("refresh_token")
