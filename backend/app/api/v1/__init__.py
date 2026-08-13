"""The /v1 router.

Note what is **not** here, and must not be added: no PUT, PATCH or DELETE on any clinical
resource. Invariant 5 makes clinical records append-only, and corrections are new rows
referencing the original (see ``CuffReadingCreate.corrects_id``).
``test_clinical_rows_have_no_update_or_delete_route`` walks the route table to keep it that way.
"""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    calibrations,
    cuff_readings,
    device,
    device_profiles,
    episodes,
    events,
    history,
    medications,
    patient_context,
    phr,
    reference,
    sessions,
)

# `profile` (module still on disk, deliberately not imported here) served `PATCH /v1/profile`.
# Invariant 5 makes clinical records append-only and this API PATCH-free everywhere else —
# `test_clinical_rows_have_no_update_or_delete_route` walks the whole route table and fails on
# any PUT/PATCH/DELETE, deliberately, so a mutable-looking route cannot slip in unnoticed. The
# tested `POST /v1/profile` in `phr.py` already does everything this route did, including the
# condition-list handling added to this module in the same change that found the problem; the
# mobile client has been pointed at that route instead. See docs/decisions.md.

# `check_sessions` (module still on disk, deliberately not imported here) duplicated
# `POST /check-sessions`, `PATCH .../preconditions`, `PATCH .../context` and
# `GET .../insight` — all four already served by `phr.router` below — and was registered
# ahead of it, so every one of those requests was going to `check_sessions.py` instead. Three
# of its four handlers wrote into the *old* schema (`clinical.CheckSession` /
# `SessionContext` / `Precondition`) while its fourth (`create_check_session`) wrote into the
# B2C one (`CheckSessionB2C`), so a session opened through this router had its preconditions
# and context recorded against a table with no row for that session's id. Its `get_insight`
# read a table (`InsightB2C`) nothing ever writes to and would 404 forever. See
# docs/decisions.md for the full account; this is the fix, not a workaround for it.

api_router = APIRouter(prefix="/v1")
api_router.include_router(auth.router)
api_router.include_router(device_profiles.router)
api_router.include_router(sessions.router)
api_router.include_router(cuff_readings.router)
api_router.include_router(patient_context.router)
api_router.include_router(phr.router)
api_router.include_router(calibrations.router)
api_router.include_router(events.router)
api_router.include_router(episodes.router)
api_router.include_router(device.router)
api_router.include_router(medications.router)
api_router.include_router(reference.router)
api_router.include_router(history.router)

__all__ = ["api_router"]
