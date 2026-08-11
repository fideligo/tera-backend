"""Server-side contraindication gate.

The handset refuses to run a spot check when the patient has reported pregnancy, and that check is
pure Dart so it survives a dead network. It is also **only a client**. An older build, a replayed
request, a second client, or anyone with a token can reach the API directly, and until now the
server would have produced a trend estimate for a patient the method was never validated on.

So the rule is enforced twice, in two places that fail independently. The handset stops the patient
early and offline; this stops the *system*, whatever asked.

# Where it refuses

- **Generating** an estimate: ``POST /v1/sessions`` refuses before anything is written.
- **Generating** the machinery for one: ``POST /v1/calibrations`` refuses. A baseline exists only
  so that estimates can be computed against it.
- **Returning** one: ``GET /v1/sessions/{id}`` withholds the trend block from a stored session.
  Estimates recorded before the patient reported pregnancy are not deleted — invariant 5 — but they
  are not served either.

# Why 403 and not a rejected session

Invariant 3 keeps sessions the system *processed and found unusable*: a signal that failed the
gate is a fact about the capture, and the clinician summary reports it. This is not that. The
refusal is a property of the patient, not of the signal, and nothing about the capture was
examined — the same shape as the existing 422 for a malformed payload and 404 for an unknown
device profile, both of which already refuse without storing.

Treating it as a rejected session would also mean writing a row whose rejection reason states a
pregnancy, on every attempt, into an append-only table.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import PatientContext, PregnancyAnswer
from app.services import language


def latest_context(db: Session, patient_id: uuid.UUID) -> PatientContext | None:
    """The context in force for a patient. ``patient_context`` is append-only, so this is the
    most recent row rather than the only one."""
    return db.execute(
        select(PatientContext)
        .where(PatientContext.patient_id == patient_id)
        .order_by(desc(PatientContext.recorded_at), desc(PatientContext.id))
        .limit(1)
    ).scalar_one_or_none()


def is_contraindicated(db: Session, patient_id: uuid.UUID) -> bool:
    """Whether trend generation is contraindicated for this patient.

    Only ``YES`` closes the gate, matching the handset exactly. ``PREFER_NOT_TO_SAY`` does not:
    blocking a declined answer makes declining functionally identical to saying yes and coerces a
    disclosure the patient chose not to make. A patient who has never filled the form in is not
    blocked either — the intake is not a precondition for using the app.

    Both of those are deliberate and both are the weak edge of this gate. See docs/decisions.md.
    """
    context = latest_context(db, patient_id)
    return context is not None and context.pregnant is PregnancyAnswer.YES


def assert_not_contraindicated(db: Session, patient_id: uuid.UUID) -> None:
    """Refuse loudly, before anything is written.

    403 rather than 422: the request is well formed and the payload is fine. What is refused is
    the operation, for this patient.
    """
    if is_contraindicated(db, patient_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=language.CONTRAINDICATED_PREGNANCY,
        )
