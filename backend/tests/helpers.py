"""Shared test helpers that need more than a fixture can express."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import make_session_payload, post_session

# --------------------------------------------------------------------------- leak detection

#: Values that are structurally incapable of being a leaked clinical value, and which a naive
#: substring search would false-positive on. A UUID is 32 hex characters, so any three-digit
#: marker has a real chance of appearing inside one — which made an earlier version of the
#: logging test pass or fail depending on which UUIDs that run happened to generate.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_ID_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]")


def _walk(node: Any, path: str = "") -> Iterator[tuple[str, Any]]:
    """Yield every leaf in a parsed JSON document, with its path."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")
    else:
        yield path, node


def find_leaked_markers(text: str, markers: tuple[str, ...]) -> list[str]:
    """Return the markers that genuinely appear as *values* in ``text``.

    ``text`` is one JSON document per line — a response body, or formatted log records.

    Numbers are compared numerically against numeric leaves; strings are searched inside string
    leaves. Identifier-shaped and timestamp-shaped leaves are skipped, because a marker turning
    up inside a UUID is a coincidence rather than a disclosure.

    Lines that are not JSON fall back to a plain substring search, which is the conservative
    behaviour: better a false alarm than a missed leak.
    """
    hits: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            hits.extend(marker for marker in markers if marker in line)
            continue

        for path, leaf in _walk(document):
            if isinstance(leaf, bool) or leaf is None:
                continue
            if isinstance(leaf, (int, float)):
                hits.extend(
                    f"{marker} (numeric, at {path})"
                    for marker in markers
                    if _numeric_match(leaf, marker)
                )
                continue
            if isinstance(leaf, str):
                if _UUID_RE.match(leaf) or _HEX_ID_RE.match(leaf) or _TIMESTAMP_RE.match(leaf):
                    continue

                # Identifiers embedded inside a longer sentence are removed before *both*
                # passes. "session 18734f2a-…-000000000187 ingested" contains the digits 187,
                # and treating that as a disclosure is how the earlier version of this check
                # became order-dependent. A genuinely leaked value is a short number sitting in
                # prose, which no identifier pattern matches.
                text = _strip_identifiers(leaf)

                # Substring: catches a value embedded in free text, e.g. a message reading
                # "rejected reading 187/113".
                hits.extend(f"{marker} (in {path})" for marker in markers if marker in text)

                # Numeric: catches a formatting difference the substring pass misses — a
                # message rendering 187 as "187.0" when the marker is "187" is caught either
                # way, but "187" against a marker of "187.0" is not.
                embedded = _NUMBER_TOKEN_RE.findall(text)
                hits.extend(
                    f"{marker} (numeric in text, at {path})"
                    for marker in markers
                    for token in embedded
                    if _numeric_match_str(token, marker)
                )

    return sorted(set(hits))


#: Number-like tokens inside a free-text string.
_NUMBER_TOKEN_RE = re.compile(r"-?\d+(?:\.\d+)?")

#: Identifiers embedded *within* a longer string would otherwise contribute spurious digit runs
#: — "session 1f0c2b7e-… failed" is not a disclosure of anything. Stripped before the numeric
#: pass; the substring pass above still sees the original text.
_EMBEDDED_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\b[0-9a-f]{16,}\b",
    re.IGNORECASE,
)


def _strip_identifiers(text: str) -> str:
    return _EMBEDDED_ID_RE.sub(" ", text)


def _numeric_match_str(token: str, marker: str) -> bool:
    try:
        return float(token) == float(marker)
    except ValueError:
        return False


def _numeric_match(leaf: float, marker: str) -> bool:
    try:
        return float(leaf) == float(marker)
    except ValueError:
        return False


def establish_calibration(
    client: TestClient,
    auth: dict[str, str],
    db,
    episode,
    device_profile,
    cuff_reading,
    *,
    ptt_targets: tuple[float, ...] = (246.0, 250.0, 254.0),
    effective_from: datetime | None = None,
) -> dict:
    """Submit three calibration sessions and establish a calibration from them.

    The sessions go through the real ingest endpoint, so a test that depends on a calibration
    also depends on the ingest path working. The calibration itself is created through the
    service with an explicit ``effective_from``, because the HTTP route stamps
    ``established_at`` at request time and most tests need the baseline to already be in force
    when their (backdated) sessions were captured. The route's own behaviour is covered by
    ``test_calibration_endpoint_establishes_a_baseline`` and the 422 cases beside it.

    Default baseline: mean 250.0, sd 4.0. With k=2 the deviation threshold is 8 ms, so a
    session at 250 ms is stable and one at 238 or 262 ms deviates.
    """
    from app.config import get_settings
    from app.services import calibration as calibration_service

    effective_from = effective_from or (datetime.now(tz=timezone.utc) - timedelta(days=25))
    base_time = effective_from - timedelta(days=3)

    session_ids = []
    for index, target in enumerate(ptt_targets):
        payload = make_session_payload(
            episode=episode,
            device_profile=device_profile,
            started_at=base_time + timedelta(days=index),
            ptt_target_ms=target,
        )
        response = post_session(client, auth, payload)
        assert response.status_code == 201, response.text
        session_ids.append(payload["session_id"])

    established = calibration_service.establish(
        db,
        patient_id=episode.patient_id,
        device_profile_id=device_profile.id,
        reference_cuff_reading_id=cuff_reading.id,
        session_ids=[uuid.UUID(s) for s in session_ids],
        settings=get_settings(),
        now=effective_from,
    )
    db.commit()
    calibration = established.calibration
    db.expire_all()

    return {
        "id": str(calibration.id),
        "baseline_mean_ms": calibration.baseline_mean_ms,
        "baseline_sd_ms": calibration.baseline_sd_ms,
        "n_sessions": calibration.n_sessions,
        "device_profile_id": str(calibration.device_profile_id),
        "reference_cuff_reading_id": str(calibration.reference_cuff_reading_id),
        "established_at": calibration.established_at.isoformat(),
        "source_session_ids": session_ids,
    }


def new_session_id() -> str:
    return str(uuid.uuid4())
