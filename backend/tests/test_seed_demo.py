"""The seeded demonstration episode.

BUILD_SPEC 4.6 specifies what the episode must contain. These tests assert it actually does,
because the demo is the artefact most likely to be looked at and least likely to be tested.

The seeder runs once for the module and every figure the tests need is captured while the rows
still exist — the autouse truncation fixture clears the database between tests, so a test that
queried afterwards would be asserting against an empty schema (which is how the first draft of
this file passed one assertion it should have failed).
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.cli import seed_demo
from app.config import get_settings
from app.db import session_scope


@pytest.fixture(scope="module")
def seeded() -> dict:
    """Run the full seeder once and snapshot everything the assertions need."""
    settings = get_settings()

    with session_scope() as db:
        ctx = seed_demo._bootstrap(db, settings)
        cuff = seed_demo._cuff_reading(ctx, day=0, hour=9, systolic=156, diastolic=96, pulse=78)
        seed_demo._seed_calibration_phase(ctx, cuff)
        seed_demo._seed_routine_phase(ctx)
        seed_demo._seed_medication_events(ctx)
        seed_demo._seed_symptom_event(ctx)
        confirmation = seed_demo._seed_deviation_sequence(ctx)
        seed_demo._seed_recalibration(ctx, confirmation)
        seed_demo._seed_post_recalibration_phase(ctx)
        seed_demo._top_up_rejections(ctx)

        db.flush()
        episode_id = ctx.episode.id
        patient_id = ctx.patient.id

        def scalar(query: str) -> int:
            return db.execute(
                sa.text(query), {"ep": episode_id, "pt": patient_id}
            ).scalar_one()

        snapshot = {
            "accepted": ctx.accepted_count,
            "rejected": ctx.rejected_count,
            "rejection_rate": ctx.rejection_rate,
            "rejections_by_reason": dict(
                db.execute(
                    sa.text(
                        "SELECT rejection_reason, count(*) FROM measurement_session "
                        "WHERE episode_id = :ep AND status = 'rejected' GROUP BY 1"
                    ),
                    {"ep": episode_id},
                ).all()
            ),
            "calibrations": scalar(
                "SELECT count(*) FROM calibration WHERE patient_id = :pt"
            ),
            "superseded_calibrations": scalar(
                "SELECT count(*) FROM calibration WHERE patient_id = :pt "
                "AND status = 'superseded'"
            ),
            "completed_sessions": scalar(
                "SELECT count(*) FROM measurement_session "
                "WHERE episode_id = :ep AND status = 'completed'"
            ),
            "cuff_readings": scalar(
                "SELECT count(*) FROM cuff_reading WHERE episode_id = :ep"
            ),
            "medication_events": scalar(
                "SELECT count(*) FROM medication_event WHERE episode_id = :ep"
            ),
            "symptom_events": scalar(
                "SELECT count(*) FROM symptom_event WHERE episode_id = :ep"
            ),
            "possible_deviations": scalar(
                "SELECT count(*) FROM trend_estimate te JOIN measurement_session ms "
                "ON ms.id = te.session_id WHERE ms.episode_id = :ep "
                "AND te.deviation_state = 'possible'"
            ),
            "persistent_deviations": scalar(
                "SELECT count(*) FROM trend_estimate te JOIN measurement_session ms "
                "ON ms.id = te.session_id WHERE ms.episode_id = :ep "
                "AND te.deviation_state = 'persistent'"
            ),
            "completed_without_estimate": scalar(
                "SELECT count(*) FROM measurement_session ms "
                "LEFT JOIN trend_estimate te ON te.session_id = ms.id "
                "WHERE ms.episode_id = :ep AND ms.status = 'completed' AND te.id IS NULL"
            ),
            "unflagged_synthetic": scalar(
                "SELECT (SELECT count(*) FROM measurement_session WHERE NOT synthetic) "
                "+ (SELECT count(*) FROM trend_estimate WHERE NOT synthetic) "
                "+ (SELECT count(*) FROM cuff_reading WHERE NOT synthetic) "
                "+ (SELECT count(*) FROM calibration WHERE NOT synthetic) "
                "+ (SELECT count(*) FROM medication_event WHERE NOT synthetic) "
                "+ (SELECT count(*) FROM symptom_event WHERE NOT synthetic) "
                "+ (SELECT count(*) FROM patient WHERE NOT synthetic) "
                "+ (SELECT count(*) FROM monitoring_episode WHERE NOT synthetic) "
                "+ (SELECT count(*) FROM device_profile WHERE NOT synthetic)"
            ),
        }

    return snapshot


# --------------------------------------------------------------------------- session yield


def test_achieved_rejection_rate_matches_the_configured_target(seeded) -> None:
    """The headline yield figure must not move with the random seed.

    The per-attempt retry draw is stochastic and on this seed lands around 23%; the seeder tops
    the shortfall up deterministically so the demo shows the configured rate every time.
    """
    total = seeded["accepted"] + seeded["rejected"]
    achieved = seeded["rejected"] / total

    assert achieved == pytest.approx(seeded["rejection_rate"], abs=0.04), (
        f"achieved {achieved:.0%} against a target of {seeded['rejection_rate']:.0%}"
    )


def test_default_rejection_rate_is_conservative() -> None:
    """The default models unsupervised home use, not controlled conditions.

    The MVP's ~80% usable target is stated for controlled seated conditions. A seeded episode
    showing that yield for a 52-year-old self-administering at home would be claiming the hard
    part of the problem is already solved.
    """
    assert 0.28 <= seed_demo.DEFAULT_REJECTION_RATE <= 0.38, (
        "the default rejection rate should sit around 30-35%"
    )


def test_rejection_reasons_are_weighted_toward_motion_and_placement(seeded) -> None:
    """The failure modes that dominate unsupervised capture should dominate the episode."""
    counts = seeded["rejections_by_reason"]
    total = sum(counts.values())
    assert total > 0

    motion_and_placement = sum(
        counts.get(reason, 0)
        for reason in (
            "excessive_motion",
            "posture_unstable",
            "poor_signal_quality",
            "insufficient_beats",
        )
    )
    assert motion_and_placement / total >= 0.6, (
        f"motion and placement account for only {motion_and_placement}/{total} rejections"
    )


def test_every_rejection_reason_is_represented(seeded) -> None:
    """The clinician summary's per-reason breakdown needs something to break down."""
    seen = set(seeded["rejections_by_reason"])
    expected = {reason.value for reason, _ in seed_demo.REJECTION_WEIGHTS}
    assert seen == expected, f"missing: {expected - seen}"


def test_rejection_weights_sum_to_one() -> None:
    assert sum(weight for _, weight in seed_demo.REJECTION_WEIGHTS) == pytest.approx(1.0)


# --------------------------------------------------------------------------- episode contents


def test_the_episode_contains_what_the_spec_asks_for(seeded) -> None:
    """BUILD_SPEC 4.6, item by item."""
    # One calibration plus one recalibration, so supersession is exercised.
    assert seeded["calibrations"] == 2
    assert seeded["superseded_calibrations"] == 1

    # Roughly thirty routine sessions.
    assert 28 <= seeded["completed_sessions"] <= 45

    # Several rejected sessions across different reasons.
    assert len(seeded["rejections_by_reason"]) >= 5

    # A handful of cuff readings, a medication log, one symptom event.
    assert 4 <= seeded["cuff_readings"] <= 8
    assert seeded["medication_events"] > 15
    assert seeded["symptom_events"] == 1

    # One deviation -> repeat -> cuff-confirmation sequence.
    assert seeded["possible_deviations"] >= 1
    assert seeded["persistent_deviations"] >= 1


def test_calibration_sessions_produced_no_estimate(seeded) -> None:
    """Invariant 7 — the three baseline-building captures predate any calibration."""
    assert seeded["completed_without_estimate"] == 3


@pytest.mark.invariant
def test_no_seeded_row_is_left_unflagged(seeded) -> None:
    """Invariant 9 — every seeded row carries synthetic: true."""
    assert seeded["unflagged_synthetic"] == 0
