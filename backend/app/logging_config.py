"""Structured logging with a clinical-content deny-list.

BUILD_SPEC 4.5: logs may carry session id, device profile id, model version, rates, gate outcome
and timings — never pressure values, PTT values, symptom text or medication detail.

The deny-list is enforced rather than documented. A developer who logs
``extra={"systolic_mmhg": 148}`` gets ``[redacted]`` in the output, not a leak. Add to
``DENIED_FIELDS`` rather than working around it.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

REDACTED = "[redacted]"

#: Field names that must never reach a log sink. Matched case-insensitively against the whole
#: field name and as a substring, so ``patient_systolic_mmhg`` is caught by ``systolic``.
DENIED_FIELDS: frozenset[str] = frozenset(
    {
        # Invariant 1 — pressure values live only in cuff_reading, never in a log line.
        "systolic",
        "diastolic",
        "systolic_mmhg",
        "diastolic_mmhg",
        "mmhg",
        "pulse_bpm",
        "blood_pressure",
        # Invariant 2 — no waveform-adjacent series, and no per-beat intervals.
        "ptt_ms",
        "ptt",
        "ptt_values",
        "waveform",
        "samples",
        "roi_series",
        "frames",
        "accel_samples",
        "baseline_mean_ms",
        "baseline_sd_ms",
        "session_ptt_ms",
        "magnitude_sd",
        # Clinical free text and medication detail.
        "symptom_text",
        "symptoms",
        "notes",
        "payload",
        "contents",
        "medication",
        "medications",
        "medication_name",
        "dose",
        "dosage",
        # B2C intake context. Pregnancy and rhythm history are clinical facts about a person,
        # not system state, and belong in the record rather than in a log line.
        "pregnant",
        "pregnancy",
        "arrhythmia",
        "known_arrhythmia",
        "regimen",
        "last_regimen_change_date",
        # Credentials.
        "password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "secret",
        "nonce",
    }
)

#: Attributes the stdlib puts on every LogRecord; anything else is treated as structured context.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info", "thread",
        "threadName", "taskName",
    }
)


def is_denied(field_name: str) -> bool:
    """Return True if ``field_name`` must never be logged."""
    lowered = field_name.lower()
    if lowered in DENIED_FIELDS:
        return True
    # Substring match catches prefixed and suffixed variants (patient_systolic, systolic_value).
    return any(denied in lowered for denied in DENIED_FIELDS)


def _scrub(value: Any, depth: int = 0) -> Any:
    """Recursively redact denied keys inside nested structures."""
    if depth > 6:  # defensive: bound recursion on pathological structures
        return REDACTED
    if isinstance(value, dict):
        return {
            key: REDACTED if is_denied(str(key)) else _scrub(val, depth + 1)
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value


#: A database error carries the row's content twice over. SQLAlchemy appends the failing
#: statement and its bound parameters, and Postgres itself prepends a DETAIL line containing
#: every column of the offending row as positional values:
#:
#:     (psycopg.errors.CheckViolation) new row for relation "cuff_reading" violates check
#:     constraint "ck_cuff_systolic_above_diastolic"
#:     DETAIL:  Failing row contains (bd2cfbf5-…, 113, 187, 133, manual_entry, …).
#:     [SQL: INSERT INTO cuff_reading (…) VALUES (…)]
#:     [parameters: {'sys': 113, 'dia': 187, 'pulse': 133}]
#:
#: The DETAIL line is the dangerous one, and it comes *first* — truncating only at ``[SQL:``
#: leaves the whole row in place. Everything from the earliest marker is dropped; the useful
#: part, the name of the constraint that failed, precedes all of them.
_SQL_ARTEFACT_MARKERS = (
    "DETAIL:",
    "HINT:",
    "CONTEXT:",
    "[SQL:",
    "[parameters:",
    "[SQL parameters:",
)

#: Matches ``key: value``, ``'key': value`` and ``key=value`` where the key contains a denied
#: name, and captures the value so it can be replaced. Covers quoted strings, bracketed lists,
#: braced dicts and bare tokens — the shapes a value takes in an exception message or an f-string.
_DENIED_ALTERNATION = "|".join(
    re.escape(name) for name in sorted(DENIED_FIELDS, key=len, reverse=True)
)
_KEY_VALUE_PATTERN = re.compile(
    r"(?P<key>['\"]?[\w.]*(?:" + _DENIED_ALTERNATION + r")\w*['\"]?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>'[^']*'|\"[^\"]*\"|\[[^\]]{0,4000}\]|\{[^}]{0,4000}\}|[^,;)}\]\s]+)",
    re.IGNORECASE,
)


def scrub_text(text: str) -> str:
    """Redact clinical content from a free-text string.

    Structured ``extra=`` fields are handled by key name, but a message built with an f-string
    or an exception rendered with ``str(exc)`` is just text. This catches the two shapes that
    actually occur: SQLAlchemy's statement/parameter blocks, and ``key: value`` pairs whose key
    is on the deny-list.

    It is a backstop, not a licence to interpolate clinical values into log messages.
    """
    if not text:
        return text

    earliest = min(
        (text.find(marker) for marker in _SQL_ARTEFACT_MARKERS if marker in text),
        default=-1,
    )
    if earliest != -1:
        text = text[:earliest].rstrip() + f" {REDACTED}"

    return _KEY_VALUE_PATTERN.sub(
        lambda match: f"{match.group('key')}{match.group('sep')}{REDACTED}", text
    )


def _exception_summary(exc_info: tuple) -> dict[str, Any]:
    """Summarise an exception without rendering its message.

    ``str(exc)`` is exactly where clinical values end up — see the SQLAlchemy note above — so it
    is never emitted. The type and the frame list are enough to locate the fault, and the frames
    carry source lines (code) rather than values (data).
    """
    exc_type, exc_value, exc_tb = exc_info
    frames = [
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(exc_tb)
    ][-12:]  # the innermost frames are the informative ones
    del exc_value
    return {
        "type": exc_type.__name__ if exc_type else "Exception",
        "frames": frames,
    }


class RedactingJsonFormatter(logging.Formatter):
    """Emit one JSON object per record, with denied fields redacted."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": scrub_text(record.getMessage()),
        }

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key.startswith("_"):
                continue
            payload[key] = REDACTED if is_denied(key) else _scrub(value)

        if record.exc_info:
            payload["exception"] = _exception_summary(record.exc_info)
        if record.stack_info:
            payload["stack"] = REDACTED

        return json.dumps(payload, default=lambda obj: scrub_text(str(obj)))


def configure_logging(level: str = "INFO") -> None:
    """Install the redacting JSON handler as the only root handler."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(RedactingJsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own colourised handlers; route them through ours so request logs are
    # scrubbed too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger for ``name``.

    Use ``extra=`` for structured context. Denied field names are redacted on the way out, so
    passing one is a bug that fails loudly in review rather than quietly in production.

    Deliberately a plain ``Logger`` and not a ``LoggerAdapter``: the adapter's ``process()``
    overwrites ``kwargs["extra"]`` with its own dict, which would silently discard every
    structured field the call site passed.
    """
    return logging.getLogger(name)
