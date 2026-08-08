"""Allow an audit entry with no role.

A failed login is the single most important thing an audit trail can record — it is how brute
force and credential stuffing become visible — and it is precisely the event with no
authenticated principal behind it. When the attempted subject does not exist there is no role
to record, and inventing one would put a false claim in an append-only log.

So ``audit_log.role`` becomes nullable, meaning "this actor was not authenticated". The actor
column still holds the attempted subject, which is what makes repeated failures against one
account visible.

Revision ID: 0004_audit_unauthenticated_actor
Revises: 0003_auth_audit_actions
Create Date: 2026-08-08

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_audit_unauthenticated_actor"
down_revision: str | None = "0003_auth_audit_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = sa.Enum("patient", "clinician", "admin", name="user_role", create_type=False)


def upgrade() -> None:
    op.alter_column("audit_log", "role", existing_type=_ROLE, nullable=True)


def downgrade() -> None:
    # Rows written for unauthenticated actors have no role to restore. Deleting them to satisfy
    # the NOT NULL would destroy audit history, which is worse than leaving the column wide, so
    # the downgrade refuses rather than doing either quietly.
    remaining = op.get_bind().execute(
        sa.text("SELECT count(*) FROM audit_log WHERE role IS NULL")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{remaining} audit entries have no role (unauthenticated actors). Reverting this "
            f"migration would require deleting them, which an append-only audit log does not "
            f"permit. Resolve deliberately before downgrading."
        )
    op.alter_column("audit_log", "role", existing_type=_ROLE, nullable=False)
