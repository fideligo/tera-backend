"""Append-only audit logging (invariant 5)."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.models import AuditAction, AuditLog
from app.security.tokens import Principal


def record(
    session: Session,
    *,
    principal: Principal,
    action: AuditAction,
    target: str | uuid.UUID | None = None,
) -> None:
    """Append one audit entry.

    ``target`` is an opaque row identifier and nothing else. The audit log records *that* a
    clinical action happened, never what the clinical content was — a log that quotes a blood
    pressure has just created a second copy of it outside the table that is supposed to hold it.
    """
    session.add(
        AuditLog(
            actor=principal.subject,
            role=principal.role,
            action=action,
            target=str(target) if target is not None else None,
        )
    )
