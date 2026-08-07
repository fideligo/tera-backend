"""Invariants 9 and 10 — synthetic labelling, and thresholds as configuration.

9. No fabricated data presented as real. Seeded and synthetic data must be unmistakably
   labelled as such in the API, the UI, and the database.
10. All clinical thresholds are configuration with documented defaults, never hard-coded magic
    numbers.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import (
    DeviationSettings,
    DeviceEligibilitySettings,
    PlausibilitySettings,
    SecuritySettings,
    get_settings,
    reset_settings_cache,
)
from app.models import CLINICAL_TABLES
from app.services import language
from tests.conftest import make_session_payload, post_session
from tests.helpers import establish_calibration


# --------------------------------------------------------------------------- invariant 9


#: Tables that carry no ``synthetic`` column, and why. Anything not listed here must have one.
SYNTHETIC_FLAG_EXEMPT = {
    # Not a record *about* a patient — it records who did what. An audit entry describes an
    # action, and the action really did happen even when its subject is synthetic.
    "audit_log": "records actions, not clinical content",
    # A join table. Its synthetic-ness is entirely determined by the calibration it belongs to,
    # and a separate flag could disagree with its parent — which is worse than not having one.
    "calibration_source_session": "join table; the parent calibration carries the flag",
}


@pytest.mark.invariant
def test_every_clinical_table_has_a_synthetic_column(db) -> None:
    """The flag lives in the database, not only in the API layer."""
    inspector = sa.inspect(db.get_bind())

    entity_tables = [
        *(t for t in CLINICAL_TABLES if t not in SYNTHETIC_FLAG_EXEMPT),
        "patient",
        "monitoring_episode",
        "device_profile",
        "calibration",
        "app_user",
    ]

    for table in entity_tables:
        columns = {c["name"] for c in inspector.get_columns(table)}
        assert "synthetic" in columns, f"{table} has no synthetic column"


@pytest.mark.invariant
def test_synthetic_exemptions_are_deliberate(db) -> None:
    """An exempt table must actually lack the column.

    Otherwise the exemption list rots into a place where a table quietly stops being checked.
    """
    inspector = sa.inspect(db.get_bind())
    for table in SYNTHETIC_FLAG_EXEMPT:
        columns = {c["name"] for c in inspector.get_columns(table)}
        assert "synthetic" not in columns, (
            f"{table} has a synthetic column now — remove it from SYNTHETIC_FLAG_EXEMPT so it "
            f"is checked like every other table"
        )


@pytest.mark.invariant
def test_synthetic_defaults_to_false(db, episode, device_profile) -> None:
    """Real data is the default; being synthetic takes a deliberate act."""
    assert episode.synthetic is False
    assert device_profile.synthetic is False


@pytest.mark.invariant
def test_seeded_rows_are_flagged_synthetic_everywhere(
    client: TestClient, auth, clinician_auth, db, episode, device_profile, cuff_reading
) -> None:
    """A synthetic session surfaces the flag *and* the notice at every layer it appears in."""
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    payload = make_session_payload(episode=episode, device_profile=device_profile)
    payload["synthetic"] = True
    response = post_session(client, auth, payload)
    assert response.status_code == 201, response.text

    # 1. the ingest response
    body = response.json()
    assert body["synthetic"] is True
    assert body["synthetic_notice"] == language.SYNTHETIC_BADGE

    # 2. session detail
    detail = client.get(f"/v1/sessions/{payload['session_id']}", headers=auth).json()
    assert detail["synthetic"] is True
    assert detail["synthetic_notice"] == language.SYNTHETIC_BADGE

    # 3. the timeline, per item and at the page level
    timeline = client.get(f"/v1/episodes/{episode.id}/timeline", headers=auth).json()
    assert timeline["contains_synthetic_data"] is True
    assert timeline["synthetic_notice"] == language.SYNTHETIC_BADGE
    estimates = [i for i in timeline["items"] if i["record_type"] == "trend_estimate"]
    synthetic_estimates = [i for i in estimates if i["synthetic"]]
    assert synthetic_estimates, "the estimate derived from a synthetic session must be flagged"
    assert all(i["synthetic_notice"] == language.SYNTHETIC_BADGE for i in synthetic_estimates)

    # 4. the clinician summary
    summary = client.get(
        f"/v1/episodes/{episode.id}/summary", headers=clinician_auth
    ).json()
    assert summary["synthetic"] is True
    assert summary["synthetic_notice"] == language.SYNTHETIC_BADGE

    # 5. the database row itself
    stored_synthetic = db.execute(
        sa.text("SELECT synthetic FROM measurement_session WHERE id = :id"),
        {"id": payload["session_id"]},
    ).scalar_one()
    assert stored_synthetic is True


@pytest.mark.invariant
def test_real_rows_carry_no_synthetic_notice(
    client: TestClient, auth, db, episode, device_profile, cuff_reading
) -> None:
    """The notice appears only for synthetic data, so it never becomes background noise."""
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    body = post_session(
        client, auth, make_session_payload(episode=episode, device_profile=device_profile)
    ).json()

    assert body["synthetic"] is False
    assert body["synthetic_notice"] is None


@pytest.mark.invariant
def test_seed_demo_marks_every_row_synthetic(db) -> None:
    """Run the seeder and assert no unflagged clinical row was written.

    BUILD_SPEC 4.6: "Every seeded row must carry ``synthetic: true``."
    """
    from app.cli import seed_demo

    with db.begin_nested():
        pass  # ensure a clean transaction boundary before the seeder opens its own

    settings = get_settings()
    seed_demo._bootstrap  # noqa: B018 - referenced so an accidental rename fails here

    from app.db import session_scope

    with session_scope() as seed_session:
        ctx = seed_demo._bootstrap(seed_session, settings)
        cuff = seed_demo._cuff_reading(
            ctx, day=0, hour=9, systolic=156, diastolic=96, pulse=78
        )
        seed_demo._seed_calibration_phase(ctx, cuff)
        seed_demo._seed_medication_events(ctx)
        seed_demo._seed_symptom_event(ctx)

    tables = (
        "patient", "monitoring_episode", "device_profile", "calibration", "cuff_reading",
        "measurement_session", "trend_estimate", "medication_event", "symptom_event",
    )
    for table in tables:
        unflagged = db.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE synthetic IS NOT TRUE")  # noqa: S608
        ).scalar_one()
        assert unflagged == 0, f"{table} has {unflagged} seeded row(s) not marked synthetic"


# --------------------------------------------------------------------------- invariant 10


@pytest.mark.invariant
@pytest.mark.parametrize(
    "settings_class",
    [DeviationSettings, PlausibilitySettings, DeviceEligibilitySettings, SecuritySettings],
)
def test_every_threshold_lives_in_config(settings_class) -> None:
    """Each settings class actually declares fields, and each has a documented default."""
    fields = settings_class.model_fields
    assert fields, f"{settings_class.__name__} declares no settings"
    for name, field in fields.items():
        assert field.default is not None or field.default_factory is not None, (
            f"{settings_class.__name__}.{name} has no default; BUILD_SPEC 4.10 requires "
            f"documented defaults."
        )


@pytest.mark.invariant
def test_config_defaults_carry_source_comments() -> None:
    """Every settings class is preceded by explanatory comments in the source.

    Invariant 10 asks that "every default carries a source comment explaining where it came
    from". This checks the comments exist and are substantial, which a reviewer then reads —
    it cannot check that they are *true*.
    """
    from app import config

    source = inspect.getsource(config)
    comment_lines = [line for line in source.splitlines() if line.strip().startswith("#")]
    assert len(comment_lines) >= 40, (
        f"app/config.py has only {len(comment_lines)} comment lines; every threshold needs a "
        f"source comment."
    )

    # And the module says plainly which numbers are cited and which are design choices.
    assert "design choice" in source.lower()
    assert "invariant 9" in source.lower()


@pytest.mark.invariant
def test_thresholds_come_from_config_not_literals(
    client: TestClient, auth, db, episode, device_profile, cuff_reading, monkeypatch
) -> None:
    """Change a threshold and the behaviour must change with it.

    This is the test that would catch a literal ``2`` inlined in the deviation engine: with
    ``deviation_k`` raised to 20, the session below stops being a deviation. If the behaviour
    did not follow the setting, the setting is decorative.
    """
    establish_calibration(client, auth, db, episode, device_profile, cuff_reading)

    deviating = make_session_payload(
        episode=episode, device_profile=device_profile, ptt_target_ms=230.0
    )
    body = post_session(client, auth, deviating).json()
    assert body["trend"]["direction"] == "increase", "baseline behaviour, k = 2"

    # Per-episode override is the documented mechanism (BUILD_SPEC 4.1 protocol_params).
    db.execute(
        sa.text("UPDATE monitoring_episode SET protocol_params = :p WHERE id = :id"),
        {"p": '{"deviation_k": 20, "min_beat_count": 30}', "id": episode.id},
    )
    db.commit()

    same_again = make_session_payload(
        episode=episode,
        device_profile=device_profile,
        ptt_target_ms=230.0,
        started_at=datetime.now(tz=timezone.utc) + timedelta(seconds=1),
    )
    body = post_session(client, auth, same_again).json()
    assert body["trend"]["direction"] == "stable", (
        "raising deviation_k did not change the verdict, so k is not really configuration"
    )


@pytest.mark.invariant
def test_min_beat_count_is_configuration(
    client: TestClient, auth, db, episode, device_profile
) -> None:
    """The usable-beat floor follows the per-episode setting."""
    payload = make_session_payload(
        episode=episode, device_profile=device_profile, n_beats=20,
        ptt_ms=[250.0 + (i % 3) for i in range(20)],
    )
    response = post_session(client, auth, payload)
    assert response.status_code == 422, "20 beats is below the default floor of 30"
    assert "below the minimum of 30" in response.text

    db.execute(
        sa.text("UPDATE monitoring_episode SET protocol_params = :p WHERE id = :id"),
        {"p": '{"min_beat_count": 10}', "id": episode.id},
    )
    db.commit()

    payload = make_session_payload(
        episode=episode, device_profile=device_profile, n_beats=20,
        ptt_ms=[250.0 + (i % 3) for i in range(20)],
    )
    assert post_session(client, auth, payload).status_code == 201


@pytest.mark.invariant
def test_ptt_range_is_configuration(
    client: TestClient, auth, episode, device_profile, monkeypatch
) -> None:
    """The plausible PTT range follows configuration too."""
    monkeypatch.setenv("TERA_PLAUSIBILITY_PTT_MIN_MS", "200")
    reset_settings_cache()
    try:
        assert get_settings().plausibility.ptt_min_ms == 200.0

        values = [250.0] * 49 + [150.0]  # inside 80-400, outside 200-400
        response = post_session(
            client,
            auth,
            make_session_payload(
                episode=episode, device_profile=device_profile, ptt_ms=values, n_beats=50
            ),
        )
        assert response.status_code == 422
        assert "200" in response.text
    finally:
        monkeypatch.delenv("TERA_PLAUSIBILITY_PTT_MIN_MS", raising=False)
        reset_settings_cache()


@pytest.mark.invariant
def test_confidence_ceiling_cannot_be_raised_toward_certainty() -> None:
    """The one threshold in the system that is a limit, not a default.

    Every other clinical threshold is tunable because a clinic may legitimately disagree with
    the default. This one is not: raising it toward 1.0 would not change what the number is —
    a blunt ordering of sessions by usable signal — but it would change what it looks like, and
    a reader who sees 0.99 reads certainty into a heuristic that cannot support it.
    """
    from app.config import CONFIDENCE_CEILING_LIMIT, DeviationSettings

    assert CONFIDENCE_CEILING_LIMIT < 1.0

    with pytest.raises(ValidationError):
        DeviationSettings(confidence_ceiling=0.999)
    with pytest.raises(ValidationError):
        DeviationSettings(confidence_ceiling=1.0)
    with pytest.raises(ValidationError):
        DeviationSettings(confidence_ceiling=CONFIDENCE_CEILING_LIMIT + 0.01)

    # Lowering it is always allowed. There is no floor on modesty.
    assert DeviationSettings(confidence_ceiling=0.5).confidence_ceiling == 0.5


@pytest.mark.invariant
def test_confidence_ceiling_limit_cannot_be_raised_by_environment(monkeypatch) -> None:
    """The bound holds against the env-var path too, not just direct construction."""
    from app.config import DeviationSettings

    monkeypatch.setenv("TERA_DEVIATION_CONFIDENCE_CEILING", "0.999")
    with pytest.raises(ValidationError):
        DeviationSettings()


@pytest.mark.invariant
@pytest.mark.parametrize(
    "overrides",
    [
        # An inverted or empty scale.
        {"confidence_floor": 0.9, "confidence_ceiling": 0.5},
        {"confidence_floor": 0.95, "confidence_ceiling": 0.95},
        # Weights that do not span floor-to-ceiling.
        {"confidence_beat_weight": 0.9, "confidence_quality_weight": 0.9},
        {"confidence_beat_weight": 0.1, "confidence_quality_weight": 0.1},
        # An inverted SNR range.
        {"confidence_snr_db_floor": 30.0, "confidence_snr_db_ceiling": 10.0},
    ],
)
def test_incoherent_confidence_settings_fail_at_startup(overrides) -> None:
    """A bad configuration would still produce numbers that look like confidences.

    Which is exactly why it has to fail loudly at construction rather than degrade quietly.
    """
    from app.config import DeviationSettings

    with pytest.raises(ValidationError):
        DeviationSettings(**overrides)


@pytest.mark.invariant
def test_deviation_k_must_be_positive() -> None:
    """A negative k would invert the comparison silently."""
    from app.config import DeviationSettings

    with pytest.raises(ValidationError):
        DeviationSettings(deviation_k=0.0)
    with pytest.raises(ValidationError):
        DeviationSettings(deviation_k=-2.0)


@pytest.mark.invariant
def test_confidence_output_stays_below_the_limit_for_any_valid_config() -> None:
    """Whatever a deployment configures, no session can report certainty."""
    from app.config import CONFIDENCE_CEILING_LIMIT, DeviationSettings
    from app.services.deviation import compute_confidence

    settings = DeviationSettings(confidence_ceiling=CONFIDENCE_CEILING_LIMIT)
    perfect = compute_confidence(
        n_usable_beats=100_000,
        quality={"snr_db": 1e6, "motion_index": 0.0, "dropped_frame_pct": 0.0},
        min_usable_beats=1,
        settings=settings,
    )

    assert perfect <= CONFIDENCE_CEILING_LIMIT
    assert perfect < 1.0


@pytest.mark.invariant
def test_cuff_plausibility_db_matches_config(db) -> None:
    """The database CHECKs and the configured ranges must agree.

    A drift between them turns a clean 422 into a 500.
    """
    from app.models.clinical import (
        DIASTOLIC_MAX_MMHG,
        DIASTOLIC_MIN_MMHG,
        PULSE_MAX_BPM,
        PULSE_MIN_BPM,
        SYSTOLIC_MAX_MMHG,
        SYSTOLIC_MIN_MMHG,
    )

    settings = get_settings().plausibility
    assert (settings.systolic_min_mmhg, settings.systolic_max_mmhg) == (
        SYSTOLIC_MIN_MMHG,
        SYSTOLIC_MAX_MMHG,
    )
    assert (settings.diastolic_min_mmhg, settings.diastolic_max_mmhg) == (
        DIASTOLIC_MIN_MMHG,
        DIASTOLIC_MAX_MMHG,
    )
    assert (settings.pulse_min_bpm, settings.pulse_max_bpm) == (PULSE_MIN_BPM, PULSE_MAX_BPM)
