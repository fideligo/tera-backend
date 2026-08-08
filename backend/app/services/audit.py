"""Append-only audit logging (invariant 5).

The proposal lists "audit trails" among its design controls (Table B1). What that has to mean in
practice is that every authentication event and every clinician access to a patient's record is
attributable after the fact, including the ones that failed.

Nothing here records clinical content. An entry says *that* something happened, to which row, by
whom — never what the value was. A log that quotes a blood pressure has made a second copy of it
outside the table that is supposed to hold it.
"""

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
    """Append one audit entry for an authenticated actor.

    ``target`` is an opaque row identifier and nothing else.
    """
    session.add(
        AuditLog(
            actor=principal.subject,
            role=principal.role,
            action=action,
            target=str(target) if target is not None else None,
        )
    )


def record_unauthenticated(
    session: Session,
    *,
    actor: str,
    action: AuditAction,
    target: str | uuid.UUID | None = None,
) -> None:
    """Append an entry for an actor who was not authenticated.

    The case this exists for is the failed login — the event an audit trail most needs to
    capture, and the one with no principal behind it. ``role`` is left null rather than guessed.

    ``actor`` is the subject that was *attempted*, which may be a string an attacker chose. It
    is stored in a bounded column and never interpolated into a log message, so it cannot be
    used to forge log lines. Recording it is what makes repeated failures against one account
    visible.
    """
    session.add(
        AuditLog(
            # Bounded to the column width; an attacker-supplied subject cannot overflow it.
            actor=actor[:128],
            role=None,
            action=action,
            target=str(target) if target is not None else None,
        )
    )
