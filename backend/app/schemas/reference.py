"""The BP reference and the monitoring-gap rule (PM spec sections 12, 27, 30).

The reference is the one thing standing between "PTT moved" and "and here is what that means",
so the shape of it matters: it names a cuff reading, it does not restate one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.models.enums import BpReferenceRefreshReason, BpReferenceStatus
from app.schemas.common import SyntheticFlag, TeraModel
from app.services import language


class BpReferenceCreate(TeraModel):
    """`POST /v1/bp-reference` — make a confirmed cuff reading the active baseline.

    Takes a reading id rather than numbers. The reading has already been through the plausibility
    gate and been confirmed by a person; re-posting the values here would be a second, unguarded
    door into the only table that holds mmHg (invariant 1).
    """

    cuff_reading_id: uuid.UUID
    refresh_reason: BpReferenceRefreshReason = BpReferenceRefreshReason.FIRST_REFERENCE


class ReferenceReadingOut(TeraModel):
    """The numbers, quoted from the reading the reference points at."""

    systolic: int
    diastolic: int
    pulse: int | None = None
    measured_at: datetime
    unit: Literal["mmHg"] = "mmHg"
    badge: Literal[language.CUFF_BADGE] = language.CUFF_BADGE


class BpReferenceOut(SyntheticFlag, TeraModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    cuff_reading_id: uuid.UUID
    activated_at: datetime
    deactivated_at: datetime | None
    refresh_reason: BpReferenceRefreshReason
    status: BpReferenceStatus
    reading: ReferenceReadingOut


class BpReferenceStatusOut(TeraModel):
    """`GET /v1/bp-reference/status` — section 30's worked example, and section 27's rule.

    The handset already computes a version of this locally so the flow works offline. This is the
    server's answer, and it is the one that survives a reinstall: `last_sensor_check_at` and the
    medication-change flag are both facts the handset cannot know on a fresh install.
    """

    has_reference: bool
    needs_refresh: bool
    #: Which of section 28's refresh reasons applies, or null when no refresh is due. Named rather
    #: than described, so the client picks the wording and the server states the fact.
    reason: BpReferenceRefreshReason | None = None
    last_sensor_check_at: datetime | None = None
    current_reference: ReferenceReadingOut | None = None
    #: How old the active reference is, against the configured validity window. Returned so a
    #: client can show "set 9 days ago" without a second round trip or its own clock arithmetic.
    reference_age_days: int | None = Field(default=None, ge=0)
