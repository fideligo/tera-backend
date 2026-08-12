"""History (PM spec sections 26 and 30).

One entry type covering all four kinds of thing that appear in a patient's record, because HIST-01
renders them in a single reverse-chronological column and four parallel arrays would leave the
interleaving — and therefore the ordering — to each client.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.schemas.common import SyntheticFlag, TeraModel

HistoryEntryType = Literal["cuff_reading", "trend", "rejected", "check"]


class HistoryEntryOut(SyntheticFlag, TeraModel):
    """One thing that happened, of one of four kinds.

    **The mmHg fields are populated only for ``cuff_reading``** and are absent everywhere else —
    invariant 1, structurally: a trend entry has no field that could carry a pressure value, so no
    client can render one against an estimate however hard it tries. ``badge`` travels with the
    numbers for the same reason: a confirmed cuff reading is labelled as one wherever it appears.
    """

    id: uuid.UUID
    entry_type: HistoryEntryType
    occurred_at: datetime

    # cuff_reading only
    systolic_mmhg: int | None = None
    diastolic_mmhg: int | None = None
    pulse_bpm: int | None = None
    unit: Literal["mmHg"] | None = None
    badge: str | None = None

    # trend only. A direction and a magnitude in units of the patient's own baseline standard
    # deviation — never a pressure.
    direction: str | None = None
    magnitude_sd: float | None = None
    deviation_state: str | None = None

    # rejected only. Invariant 3: the reason is retained and reported, never dropped.
    rejection_reason: str | None = None

    # check only
    mode: str | None = None
    check_status: str | None = None


class HistoryOut(TeraModel):
    """`GET /v1/history?range=7d&type=all`."""

    range: str
    type: str
    entries: list[HistoryEntryOut] = Field(default_factory=list)
