"""Audit actions for changing a password and closing an account.

Both are account events rather than clinical ones. Closing an account deletes the `app_user` row —
the login subject and the password hash — and leaves every clinical row untouched, so invariant 5
is not in play: no append-only table is written to except the audit log itself, which is the point.

Recorded as enum values rather than free text for the same reason as 0003: a typo creates a
category nobody greps for.

Revision ID: 0014_account_lifecycle_audit
Revises: 0013_b2c_section_28
Create Date: 2026-08-18

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_account_lifecycle_audit"
down_revision: str | None = "0013_b2c_section_28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ACTIONS = (
    "auth_password_changed",
    "auth_account_closed",
)


def upgrade() -> None:
    # PostgreSQL 12+ permits ALTER TYPE ... ADD VALUE inside a transaction provided the value is
    # not used in the same transaction. This only declares them.
    for action in NEW_ACTIONS:
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{action}'")


def downgrade() -> None:
    # Postgres cannot remove a value from an enum type without rewriting it, and rewriting an enum
    # the append-only audit log depends on would mean rewriting that log. 0003 takes the same
    # position and for the same reason.
    pass
