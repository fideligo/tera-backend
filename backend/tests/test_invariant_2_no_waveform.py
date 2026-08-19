"""Invariant 2 — no raw waveform is stored or transmitted.

"Camera frames, region-of-interest intensity series, and accelerometer sample buffers never
leave the handset and are never persisted anywhere. The deepest granularity accepted by the API
is one derived interval per beat."
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.config import get_settings
from app.models.session import PTT_ARRAY_DB_CEILING
from tests.conftest import make_session_payload, post_session


@pytest.mark.invariant
def test_ptt_array_length_bound_enforced(
    client: TestClient, auth, episode, device_profile
) -> None:
    """A payload above the configured maximum is refused with 422.

    The bound is what stops ``ptt_ms`` being used to smuggle a sample buffer under the name of
    per-beat intervals.
    """
    limit = get_settings().plausibility.max_ptt_array_length
    oversized = [250.0] * (limit + 1)

    response = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode, device_profile=device_profile, ptt_ms=oversized, n_beats=limit + 1
        ),
    )

    assert response.status_code == 422, response.text
    violations = response.json()["detail"]["violations"]
    assert any(v["field"] == "ptt_ms" for v in violations), violations
    assert any(str(limit) in v["message"] for v in violations), violations


@pytest.mark.invariant
def test_ptt_array_at_the_bound_is_accepted(
    client: TestClient, auth, episode, device_profile
) -> None:
    """The bound is inclusive — exactly at the limit is fine, one over is not."""
    limit = get_settings().plausibility.max_ptt_array_length
    at_limit = [250.0 + (index % 5) * 0.5 for index in range(limit)]

    response = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode, device_profile=device_profile, ptt_ms=at_limit, n_beats=limit
        ),
    )
    assert response.status_code == 201, response.text


@pytest.mark.invariant
def test_ptt_array_db_ceiling_matches_config() -> None:
    """The configured limit must sit at or below the database CHECK.

    If someone raises ``max_ptt_array_length`` past the structural ceiling, the API would accept
    a payload the database then refuses — a 500 instead of a clean 422. This test fails first
    and says to write a migration.
    """
    configured = get_settings().plausibility.max_ptt_array_length
    assert configured <= PTT_ARRAY_DB_CEILING, (
        f"max_ptt_array_length ({configured}) exceeds the database ceiling "
        f"({PTT_ARRAY_DB_CEILING}). Raising it requires a migration that widens "
        f"ck_session_ptt_array_length_bounded."
    )


@pytest.mark.invariant
def test_ptt_array_length_bounded_at_database_level(db, episode, device_profile) -> None:
    """The ceiling holds even for a writer that bypasses the API entirely."""
    import uuid
    from datetime import datetime, timezone

    with pytest.raises(sa.exc.IntegrityError) as excinfo:
        db.execute(
            sa.text(
                """
                INSERT INTO measurement_session (
                    id, episode_id, device_profile_id, model_version, started_at, posture,
                    status, n_beats_total, n_beats_usable, ptt_ms, quality
                ) VALUES (
                    :id, :episode_id, :device_profile_id, 'direct', :started_at, 'seated',
                    'completed', :n, :n, :ptt, '{}'::jsonb
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "episode_id": episode.id,
                "device_profile_id": device_profile.id,
                "started_at": datetime.now(tz=timezone.utc),
                "n": PTT_ARRAY_DB_CEILING + 1,
                "ptt": [250.0] * (PTT_ARRAY_DB_CEILING + 1),
            },
        )
        db.flush()

    assert "ck_session_ptt_array_length_bounded" in str(excinfo.value)
    db.rollback()


@pytest.mark.invariant
def test_implausible_ptt_rejected_with_422(
    client: TestClient, auth, episode, device_profile
) -> None:
    """PTT values outside the configured physiological range are refused (BUILD_SPEC 4.4)."""
    settings = get_settings().plausibility

    for bad_value in (settings.ptt_min_ms - 1.0, settings.ptt_max_ms + 1.0):
        values = [250.0] * 49 + [bad_value]
        response = post_session(
            client,
            auth,
            make_session_payload(
                episode=episode, device_profile=device_profile, ptt_ms=values, n_beats=50
            ),
        )
        assert response.status_code == 422, (bad_value, response.text)
        violations = response.json()["detail"]["violations"]
        assert any("plausible range" in v["message"] for v in violations), violations


@pytest.mark.invariant
def test_ptt_ceiling_is_back_within_the_spec(
    client: TestClient, auth, episode, device_profile
) -> None:
    """BUILD_SPEC 4.4 says 80-400 ms, and after a brief deviation to 500 we are back inside it.

    The deviation was real: the handset's fiducials lengthen the measured interval, because aortic
    opening is backtracked to 82% of the envelope rise and the PPG foot is placed by intersecting
    tangents, both earlier than the marks the 80-400 figure was written against. A seated capture
    was losing pairs to the ceiling.

    It was retired rather than left standing. The handset has since tightened its own pairing
    ceiling to 380 ms for a better reason — at 380 the window cannot reach the following cardiac
    cycle at any rate the chain accepts — so nothing a client can now produce exceeds 380 and this
    bound has no work to do above 400.

    Still asserted, because the two must not silently drift apart: a phone that accepts what the
    server 422s is the split that already cost us `min_usable_beats`, and nothing but this test and
    the decisions entry enforces it.
    """
    settings = get_settings().plausibility
    assert settings.ptt_max_ms == 400.0
    assert settings.ptt_min_ms == 80.0

    # The handset pairs to 380 ms, so this sits above anything it can produce and a legitimate
    # session is never 422'd here. Defence in depth is being no *tighter* than the client, not
    # matching it.
    response = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            ptt_ms=[375.0] * 40,
            n_beats=40,
        ),
    )
    assert response.status_code in (200, 201), response.text


@pytest.mark.invariant
def test_a_ceiling_still_exists(
    client: TestClient, auth, episode, device_profile
) -> None:
    """Widening is not removing. Half a cardiac cycle at 60 bpm is still the limit.

    500 ms cannot admit a pair formed across two different beats, which is the property that makes
    the wider window safe rather than merely more permissive.
    """
    response = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode,
            device_profile=device_profile,
            ptt_ms=[250.0] * 39 + [900.0],
            n_beats=40,
        ),
    )
    assert response.status_code == 422, response.text


@pytest.mark.invariant
def test_error_body_does_not_echo_beat_values(
    client: TestClient, auth, episode, device_profile
) -> None:
    """A 422 counts the offending intervals; it does not list them back.

    An error body is not a place for per-beat physiological data.
    """
    values = [250.0] * 48 + [12.0, 9999.0]
    response = post_session(
        client,
        auth,
        make_session_payload(
            episode=episode, device_profile=device_profile, ptt_ms=values, n_beats=50
        ),
    )
    assert response.status_code == 422
    body = response.text
    assert "9999" not in body and "12.0" not in body


@pytest.mark.invariant
def test_no_raw_waveform_fields_accepted(
    client: TestClient, auth, episode, device_profile
) -> None:
    """Unknown fields are rejected, so a waveform cannot ride along in the payload.

    ``extra="forbid"`` on the request schemas means a client cannot add ``frames``,
    ``roi_series`` or ``accel_samples`` and have them silently ignored — or worse, stored.
    """
    for smuggled in ("frames", "roi_series", "accel_samples", "raw_waveform"):
        payload = make_session_payload(episode=episode, device_profile=device_profile)
        payload[smuggled] = [1, 2, 3, 4, 5]

        response = post_session(client, auth, payload)
        assert response.status_code == 422, (smuggled, response.text)

    # And inside the nested quality object too.
    payload = make_session_payload(episode=episode, device_profile=device_profile)
    payload["quality"]["intensity_series"] = [0.1, 0.2, 0.3]
    assert post_session(client, auth, payload).status_code == 422


@pytest.mark.invariant
def test_no_waveform_columns_in_the_schema(db) -> None:
    """No table anywhere holds a column named like a sample buffer."""
    inspector = sa.inspect(db.get_bind())
    forbidden = ("frame", "roi_", "sample_buffer", "waveform", "intensity", "raw_")

    offenders: list[str] = []
    for table in inspector.get_table_names():
        for column in inspector.get_columns(table):
            name = column["name"].lower()
            # dropped_frame_pct is a summary statistic, not a frame store.
            if name == "dropped_frame_pct":
                continue
            if any(token in name for token in forbidden):
                offenders.append(f"{table}.{column['name']}")

    assert not offenders, f"waveform-like column(s) found: {offenders}"
