"""Error paths must not leak clinical content — into logs or into response bodies.

A snapshot scan of a healthy run proves nothing about the paths that matter. The dangerous ones
are the paths nobody looks at:

* **Pydantic 422s.** Every error dict carries ``input`` — the offending value itself. FastAPI's
  default handler returns it. On this API that means a 422 echoing a blood-pressure value or a
  beat interval back to the caller.
* **Unhandled exceptions.** SQLAlchemy appends the failing statement *and its bound parameters*
  to every DBAPI error. That is a complete copy of the row's clinical content inside an
  exception message, one `str(exc)` away from the logs.
* **Anything built with an f-string.** Structured ``extra=`` fields are redacted by key name; a
  message is just text.

Every test here fires a payload carrying distinctive marker values and asserts they appear in
neither the response nor the logs.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.logging_config import REDACTED, RedactingJsonFormatter, scrub_text
from app.main import app
from tests.conftest import make_session_payload, post_session
from tests.helpers import find_leaked_markers

#: Distinctive values that appear nowhere else in the codebase or the fixtures, so finding one
#: in a log line or a response body is unambiguous evidence of a leak.
MARKER_SYSTOLIC = 187
MARKER_DIASTOLIC = 113
MARKER_PULSE = 133
MARKER_PTT = 337.71
MARKER_TEXT = "leak-canary-symptom-text-9f3a"
MARKER_MEDICATION = "leak-canary-medication-name-c71b"

ALL_MARKERS = (
    str(MARKER_SYSTOLIC),
    str(MARKER_DIASTOLIC),
    str(MARKER_PULSE),
    str(MARKER_PTT),
    MARKER_TEXT,
    MARKER_MEDICATION,
)


class _RecordCollector(logging.Handler):
    """Collect every record the app emits, formatted the way production formats them."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def rendered(self) -> str:
        formatter = RedactingJsonFormatter()
        return "\n".join(formatter.format(record) for record in self.records)


@pytest.fixture
def captured_logs():
    """Capture app logs for the duration of a test.

    Not ``caplog``: ``configure_logging`` clears the root handlers at app startup, which removes
    pytest's capture handler along with everything else.
    """
    collector = _RecordCollector()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(collector)
    root.setLevel(logging.DEBUG)
    try:
        yield collector
    finally:
        root.removeHandler(collector)
        root.setLevel(previous_level)


@pytest.fixture
def raw_client():
    """A client that returns 500 responses instead of re-raising them.

    ``TestClient`` defaults to ``raise_server_exceptions=True``, which would hide the response
    body the client actually receives — which is the thing under test.
    """
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def assert_clean(text: str, where: str, markers=ALL_MARKERS) -> None:
    """Assert none of the marker values appear as *values* in ``text``.

    Structural rather than substring: identifiers are hex, so a three-digit marker will
    eventually appear inside a UUID or an ``incident_id`` by chance. Field *names* may
    legitimately appear too — a validation message has to say which field was wrong. What must
    never appear is a value.
    """
    leaks = find_leaked_markers(text, markers)
    assert not leaks, f"{where} leaked clinical value(s): {leaks}"


# --------------------------------------------------------------------------- validation paths


@pytest.mark.invariant
def test_cuff_reading_422_does_not_echo_the_submitted_values(
    raw_client: TestClient, auth, episode, captured_logs
) -> None:
    """An implausible reading is refused without quoting the numbers back."""
    taken_at = datetime.now(tz=timezone.utc)
    response = raw_client.post(
        "/v1/cuff-readings",
        headers=auth,
        json={
            "episode_id": str(episode.id),
            # Diastolic above systolic: fails the plausibility gate, not the parser.
            "systolic_mmhg": MARKER_DIASTOLIC,
            "diastolic_mmhg": MARKER_SYSTOLIC,
            "pulse_bpm": MARKER_PULSE,
            "source": "manual_entry",
            "taken_at": taken_at.isoformat(),
            "user_confirmed_at": taken_at.isoformat(),
        },
    )

    assert response.status_code == 422
    assert_clean(response.text, "cuff reading 422 body")
    assert_clean(captured_logs.rendered(), "logs during cuff reading 422")


@pytest.mark.invariant
def test_pydantic_422_does_not_echo_the_input_value(
    raw_client: TestClient, auth, episode, captured_logs
) -> None:
    """The parser-level 422 is the one that echoes by default.

    Pydantic's error dicts carry ``input``. If FastAPI's default handler were in place this body
    would contain the submitted pressure value.
    """
    taken_at = datetime.now(tz=timezone.utc)
    response = raw_client.post(
        "/v1/cuff-readings",
        headers=auth,
        json={
            "episode_id": str(episode.id),
            "systolic_mmhg": 99187,  # above the field's own le=1000, so Pydantic rejects it
            "diastolic_mmhg": MARKER_DIASTOLIC,
            "source": "manual_entry",
            "taken_at": taken_at.isoformat(),
            "user_confirmed_at": taken_at.isoformat(),
        },
    )

    assert response.status_code == 422
    body = response.json()
    assert body["violations"], "the client still needs to know which field was wrong"
    assert body["violations"][0]["field"] == "systolic_mmhg"
    assert "99187" not in response.text, "the 422 body echoed the submitted value"
    # The `input` key is where Pydantic puts the offending value; the word "Input" also opens
    # its standard messages, so the JSON key form is what to check for.
    assert '"input"' not in response.text
    assert '"ctx"' not in response.text
    assert_clean(response.text, "pydantic 422 body")
    assert_clean(captured_logs.rendered(), "logs during pydantic 422")


@pytest.mark.invariant
def test_session_422_does_not_echo_beat_intervals(
    raw_client: TestClient, auth, episode, device_profile, captured_logs
) -> None:
    """An implausible PTT array is counted, never listed back."""
    values = [250.0] * 49 + [MARKER_PTT * 10]
    payload = make_session_payload(
        episode=episode, device_profile=device_profile, ptt_ms=values, n_beats=50
    )
    nonce = raw_client.post("/v1/sessions/nonce", headers=auth).json()["nonce"]
    response = raw_client.post(
        "/v1/sessions",
        json=payload,
        headers={
            **auth,
            "X-Session-Nonce": nonce,
            "Idempotency-Key": payload["session_id"],
        },
    )

    assert response.status_code == 422
    assert "3377" not in response.text
    assert_clean(response.text, "session 422 body")
    assert_clean(captured_logs.rendered(), "logs during session 422")


@pytest.mark.invariant
def test_event_payload_never_reaches_logs_or_error_bodies(
    raw_client: TestClient, auth, episode, captured_logs
) -> None:
    """Symptom text and medication detail are the free-text leak vector."""
    occurred_at = datetime.now(tz=timezone.utc).isoformat()

    ok = raw_client.post(
        "/v1/events",
        headers=auth,
        json={
            "episode_id": str(episode.id),
            "event_type": "symptom",
            "occurred_at": occurred_at,
            "payload": {"symptom": MARKER_TEXT, "medication": MARKER_MEDICATION},
        },
    )
    assert ok.status_code == 201

    # And on the failure path: a payload too large for the bounded-report rule.
    rejected = raw_client.post(
        "/v1/events",
        headers=auth,
        json={
            "episode_id": str(episode.id),
            "event_type": "symptom",
            "occurred_at": occurred_at,
            "payload": {"symptom": MARKER_TEXT, "series": [MARKER_PTT] * 64},
        },
    )
    assert rejected.status_code == 422
    assert_clean(rejected.text, "event 422 body")
    assert_clean(captured_logs.rendered(), "logs during event submission")


@pytest.mark.invariant
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/device-profiles", {"accel_rate_hz": MARKER_PTT, "model": MARKER_TEXT}),
        ("POST", "/v1/sessions", {"ptt_ms": [MARKER_PTT] * 4, "n_beats_usable": 4}),
        ("POST", "/v1/cuff-readings", {"systolic_mmhg": MARKER_SYSTOLIC}),
        ("POST", "/v1/calibrations", {"session_ids": [MARKER_TEXT]}),
        ("POST", "/v1/events", {"payload": {"symptom": MARKER_TEXT}}),
    ],
)
def test_every_write_endpoint_refuses_garbage_without_echoing_it(
    raw_client: TestClient, auth, captured_logs, method, path, body
) -> None:
    """Fire a structurally invalid, clinically loaded payload at each write endpoint."""
    response = raw_client.request(method, path, headers=auth, json=body)

    assert response.status_code in (400, 422, 428), response.text
    assert_clean(response.text, f"{method} {path} error body")
    assert_clean(captured_logs.rendered(), f"logs during {method} {path}")


@pytest.mark.invariant
def test_read_endpoints_with_bad_ids_leak_nothing(
    raw_client: TestClient, auth, clinician_auth, captured_logs
) -> None:
    """Unknown and malformed ids on the read paths."""
    unknown = uuid.uuid4()
    for path, headers in (
        (f"/v1/episodes/{unknown}/timeline", auth),
        (f"/v1/episodes/{unknown}/summary", clinician_auth),
        (f"/v1/sessions/{unknown}", auth),
        (f"/v1/calibrations/{unknown}", auth),
        (f"/v1/device-profiles/{unknown}", auth),
        ("/v1/episodes/not-a-uuid/timeline", auth),
    ):
        response = raw_client.get(path, headers=headers)
        assert response.status_code in (404, 422), (path, response.text)
        assert_clean(response.text, f"GET {path}")

    assert_clean(captured_logs.rendered(), "logs during read-path errors")


# --------------------------------------------------------------------------- unhandled errors


@pytest.mark.invariant
def test_unhandled_exception_returns_generic_500_and_leaks_nothing(
    raw_client: TestClient, auth, episode, device_profile, captured_logs, monkeypatch
) -> None:
    """Force a handler to raise an exception whose message embeds clinical values.

    This is the SQLAlchemy shape: the exception text carries the row's content. The response
    must be an opaque incident id and the log must carry the type and frames but not the message.
    """
    from app.services import ingest

    def _explode(*args, **kwargs):
        raise RuntimeError(
            f"(psycopg.errors.CheckViolation) failed "
            f"[SQL: INSERT INTO cuff_reading (systolic_mmhg) VALUES (%s)] "
            f"[parameters: {{'systolic_mmhg': {MARKER_SYSTOLIC}, "
            f"'diastolic_mmhg': {MARKER_DIASTOLIC}, 'ptt_ms': [{MARKER_PTT}], "
            f"'symptom': '{MARKER_TEXT}'}}]"
        )

    monkeypatch.setattr(ingest, "submit", _explode)

    payload = make_session_payload(episode=episode, device_profile=device_profile)
    nonce = raw_client.post("/v1/sessions/nonce", headers=auth).json()["nonce"]
    response = raw_client.post(
        "/v1/sessions",
        json=payload,
        headers={
            **auth,
            "X-Session-Nonce": nonce,
            "Idempotency-Key": payload["session_id"],
        },
    )

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "internal error"
    assert body["incident_id"], "an operator needs something to grep for"
    assert "RuntimeError" not in response.text
    assert "parameters" not in response.text.lower()
    assert_clean(response.text, "500 response body")

    rendered = captured_logs.rendered()
    assert "unhandled_exception" in rendered, "the incident was not logged at all"
    assert body["incident_id"] in rendered, "the log cannot be matched to the caller's id"
    assert "RuntimeError" in rendered, "the exception type is needed to investigate"
    assert_clean(rendered, "logs during unhandled exception")


@pytest.mark.invariant
def test_database_integrity_error_does_not_leak_bound_parameters(
    raw_client: TestClient, db, episode, captured_logs
) -> None:
    """The real SQLAlchemy case, not a simulation of it.

    Provoke a genuine CHECK violation on ``cuff_reading`` and confirm the driver's message —
    which contains the bound parameters — is scrubbed on the way to the log.
    """
    with pytest.raises(sa.exc.IntegrityError) as excinfo:
        db.execute(
            sa.text(
                "INSERT INTO cuff_reading "
                "(id, episode_id, systolic_mmhg, diastolic_mmhg, pulse_bpm, source, taken_at, "
                " user_confirmed_at) "
                "VALUES (:id, :ep, :sys, :dia, :pulse, 'manual_entry', now(), now())"
            ),
            {
                "id": uuid.uuid4(),
                "ep": episode.id,
                "sys": MARKER_DIASTOLIC,   # lower than diastolic -> violates the CHECK
                "dia": MARKER_SYSTOLIC,
                "pulse": MARKER_PULSE,
            },
        )
    db.rollback()

    raw_message = str(excinfo.value)
    # Confirm the leak vector is real before asserting that it is closed.
    assert str(MARKER_SYSTOLIC) in raw_message, (
        "SQLAlchemy no longer embeds bound parameters; the scrubber may be unnecessary"
    )

    logger = logging.getLogger("app.test")
    logger.error("db_failure: %s", raw_message)

    rendered = captured_logs.rendered()
    assert "db_failure" in rendered
    assert_clean(rendered, "logs after a real IntegrityError")


# --------------------------------------------------------------------------- the scrubber


def test_scrub_text_drops_sqlalchemy_statement_and_parameters() -> None:
    text = (
        "(psycopg.errors.CheckViolation) new row violates check constraint "
        "\"ck_cuff_systolic_above_diastolic\"\n"
        "[SQL: INSERT INTO cuff_reading (systolic_mmhg) VALUES (%(systolic_mmhg)s)]\n"
        "[parameters: {'systolic_mmhg': 187, 'diastolic_mmhg': 113}]"
    )
    scrubbed = scrub_text(text)

    assert "ck_cuff_systolic_above_diastolic" in scrubbed, "the useful part must survive"
    assert "187" not in scrubbed
    assert "113" not in scrubbed
    assert "INSERT INTO" not in scrubbed
    assert REDACTED in scrubbed


@pytest.mark.parametrize(
    "text",
    [
        "systolic_mmhg: 187",
        "'diastolic_mmhg': 113",
        'reading {"systolic_mmhg": 187, "pulse_bpm": 133}',
        "ptt_ms=[337.71, 250.0]",
        "patient_systolic_value = 187",
        "symptom_text: 'leak-canary'",
        "access_token=eyJhbGciOi.secret.value",
    ],
)
def test_scrub_text_redacts_key_value_pairs(text: str) -> None:
    scrubbed = scrub_text(text)
    for value in ("187", "113", "133", "337.71", "leak-canary", "eyJhbGciOi"):
        assert value not in scrubbed, f"{text!r} -> {scrubbed!r}"


def test_scrub_text_leaves_permitted_context_alone() -> None:
    """Redaction must not empty out the fields an operator actually needs."""
    text = (
        "session_id=1f0c2b7e-0000-4000-8000-000000000001 camera_fps=58.7 "
        "accel_rate_hz=201.3 gate_outcome=completed model_version=tera-0.1.0"
    )
    assert scrub_text(text) == text


def test_find_leaked_markers_catches_a_value_embedded_in_free_text() -> None:
    """The check must not only look at numeric leaves.

    A message like "rejected reading 187/113" puts the values inside a *string*. Both the
    substring pass and the numeric-token pass have to see it, and a formatting difference
    ("187.0" against a marker of "187", or the reverse) must not let it through.
    """
    document = json.dumps(
        {"event": "gate_failed: rejected reading 187/113", "session_id": "abc"}
    )
    assert find_leaked_markers(document, ("187", "113"))

    reversed_format = json.dumps({"event": "value was 187 exactly"})
    assert find_leaked_markers(reversed_format, ("187.0",)), (
        "a marker of 187.0 must match the token 187 in free text"
    )

    padded_format = json.dumps({"event": "value was 187.00"})
    assert find_leaked_markers(padded_format, ("187",))


def test_find_leaked_markers_ignores_digits_inside_identifiers() -> None:
    """Identifiers are hex; a three-digit marker turns up inside one by chance.

    Both as a whole leaf and embedded in a longer sentence.
    """
    whole_leaf = json.dumps({"session_id": "18734f2a-1130-4000-8000-000000000187"})
    assert not find_leaked_markers(whole_leaf, ("187", "113"))

    embedded = json.dumps(
        {"event": "session 18734f2a-1130-4000-8000-000000000187 ingested"}
    )
    assert not find_leaked_markers(embedded, ("187", "113")), (
        "digits inside an embedded identifier are a coincidence, not a disclosure"
    )

    # But a real value alongside an identifier is still caught.
    both = json.dumps(
        {"event": "session 18734f2a-1130-4000-8000-000000000abc had reading 187/113"}
    )
    assert find_leaked_markers(both, ("187",))


@pytest.mark.invariant
def test_no_custom_validator_echoes_a_submitted_value(
    raw_client: TestClient, auth, episode, captured_logs
) -> None:
    """Pydantic's `msg` survives into the 422 body, so a custom validator's message must not
    contain the value that triggered it.

    The event payload is the sharp case: its *keys* are client-controlled, so a message naming
    the offending key would put caller-supplied text — possibly clinical text — into the error
    path and the logs.
    """
    response = raw_client.post(
        "/v1/events",
        headers=auth,
        json={
            "episode_id": str(episode.id),
            "event_type": "symptom",
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            # The key itself is the payload.
            "payload": {f"reading {MARKER_SYSTOLIC}/{MARKER_DIASTOLIC}": [1] * 64},
        },
    )

    assert response.status_code == 422, response.text
    assert_clean(response.text, "event validator 422 body")
    assert_clean(captured_logs.rendered(), "logs during event validator 422")


@pytest.mark.invariant
def test_validator_messages_contain_no_interpolated_values() -> None:
    """Read the validators' source and check for f-strings over user data.

    A blunt check, and deliberately so: the failure mode is someone adding
    ``f"... {value} ..."`` to a validator months from now, and a reviewer not noticing that
    validation messages are returned to the caller.
    """
    import inspect

    from app.schemas import clinical, session

    for module in (clinical, session):
        source = inspect.getsource(module)
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("raise ValueError(", 'raise ValueError(f"')):
                continue
            assert not stripped.startswith('raise ValueError(f"'), (
                f"{module.__name__} interpolates into a validation message: {stripped}. "
                f"Validation messages reach the caller and the logs — state the rule, not "
                f"the value."
            )


def test_exception_summary_omits_the_message_but_keeps_the_frames() -> None:
    """Frames locate the fault; the message is where the data would be."""
    try:
        raise ValueError(f"systolic_mmhg {MARKER_SYSTOLIC} out of range")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="boom", args=(), exc_info=sys.exc_info(),
        )

    rendered = json.loads(RedactingJsonFormatter().format(record))

    assert rendered["exception"]["type"] == "ValueError"
    assert rendered["exception"]["frames"], "frames are needed to find the fault"
    assert str(MARKER_SYSTOLIC) not in json.dumps(rendered)
    assert "out of range" not in json.dumps(rendered)
