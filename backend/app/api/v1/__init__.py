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
    device_profiles,
    episodes,
    events,
    patient_context,
    sessions,
)

api_router = APIRouter(prefix="/v1")
api_router.include_router(auth.router)
api_router.include_router(device_profiles.router)
api_router.include_router(sessions.router)
api_router.include_router(cuff_readings.router)
api_router.include_router(patient_context.router)
api_router.include_router(calibrations.router)
api_router.include_router(events.router)
api_router.include_router(episodes.router)

__all__ = ["api_router"]
