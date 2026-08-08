"""Audit actions for the auth surface.

Login failures, logout, refresh, refresh-token reuse, registration and denied clinician access
all have to be attributable. Adding them as enum values rather than free text keeps the audit
log queryable and stops a typo creating a category nobody ever greps for.

Revision ID: 0003_auth_audit_actions
Revises: 0002_refresh_token_rotation
Create Date: 2026-08-07

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_auth_audit_actions"
down_revision: str | None = "0002_refresh_token_rotation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ACTIONS = (
    "auth_login_failed",
    "auth_logout",
    "auth_token_refreshed",
    "auth_refresh_reuse_detected",
    "user_registered",
    "clinician_access_denied",
)


def upgrade() -> None:
    # PostgreSQL 12+ permits ALTER TYPE ... ADD VALUE inside a transaction, provided the new
    # value is not used in that same transaction. This migration only declares them.
    for action in NEW_ACTIONS:
        op.execute(f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{action}'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type. Reversing this would mean recreating
    # audit_action and rewriting every column that uses it, which on an append-only audit log
    # is a worse outcome than leaving six unused labels declared. Deliberately a no-op.
    pass
