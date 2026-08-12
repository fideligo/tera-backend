"""FastAPI application entry point."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1 import api_router
from app.config import get_settings
from app.logging_config import configure_logging, get_logger, scrub_text

DESCRIPTION = """
Tera is a **hybrid, cuff-referenced home blood-pressure monitoring system**. A phone captures
seismocardiography and photoplethysmography; the interval between them is the pulse transit time
(PTT), which tracks *change* in blood pressure rather than its absolute value.

**This API never returns a blood-pressure value derived from the phone.** Estimates are a
direction plus a magnitude in units of the patient's own baseline standard deviation. Only
`cuff_reading` holds mmHg, and it comes from a validated upper-arm cuff confirmed by a person.

A cuff remains the reference. Tera adds continuity between clinic visits — portability and record
completeness, not a replacement for the cuff.

The system does not diagnose, does not advise on medication, and does not offer clinical
reassurance.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    log = get_logger(__name__)

    # A default signing secret in production would make every token forgeable. Failing at
    # startup is the only safe response.
    if settings.env == "production" and "dev-only" in settings.security.jwt_secret:
        raise RuntimeError(
            "TERA_JWT_SECRET is still the development default. Set a real secret before "
            "running with TERA_ENV=production."
        )

    log.info("api_starting", extra={"env": settings.env})
    yield
    log.info("api_stopping")


app = FastAPI(
    title="Tera API",
    version="0.1.0",
    description=DESCRIPTION,
    lifespan=lifespan,
)

app.include_router(api_router)

# The PM spec writes every route as `/api/v1/...`; this API has served `/v1/...` since 0001 and the
# patient app is built against it. Both work: the same router is mounted a second time under
# `/api`, so a client following section 30 literally is not wrong either.
#
# `include_in_schema=False` on the alias, deliberately. One operation appearing twice in the
# OpenAPI schema would double `docs/api.md` and hand the invariant tests that walk that schema two
# copies of every route to reason about. The canonical path is `/v1`; the alias is a courtesy.
app.include_router(api_router, prefix="/api", include_in_schema=False)


@app.get("/health", tags=["ops"], summary="Liveness probe")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return schema validation failures in the same envelope as the plausibility gate.

    A client should not have to parse two different 422 shapes depending on whether the payload
    failed the parser or the gate.

    Only ``loc`` and ``msg`` are taken from each Pydantic error. The error dicts also carry
    ``input`` — the offending value itself — and ``ctx``, which can quote it. FastAPI's default
    handler returns both, which on this API would mean a 422 echoing a blood-pressure value or a
    beat interval straight back to the caller and into any access log that records response
    bodies. They are dropped here deliberately; ``msg`` alone says what was wrong.
    """
    del request
    violations = [
        {
            "field": ".".join(str(part) for part in error["loc"][1:]) or "body",
            "message": scrub_text(str(error["msg"])),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": "payload failed validation", "violations": violations},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a generic 500 and log an incident id, never the exception text.

    An unhandled exception is the single worst leak vector in this system. SQLAlchemy's DBAPI
    errors carry the failing statement *and* its bound parameters — a complete copy of the row's
    clinical content — and a default handler would put that in the response body, the log, or
    both.

    So the client gets an opaque incident id and nothing else, and the log gets the id, the
    exception type and the frame list. To investigate, grep the logs for the id the caller
    quotes; the frames say where to look.
    """
    incident_id = uuid.uuid4().hex
    log = get_logger(__name__)
    log.error(
        "unhandled_exception",
        extra={
            "incident_id": incident_id,
            "exception_type": type(exc).__name__,
            "method": request.method,
            "path": request.url.path,
        },
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "internal error",
            "incident_id": incident_id,
        },
    )
