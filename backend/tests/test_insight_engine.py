"""The intervention rule engine (PM spec section 24).

Pure functions, so these are rules tested as rules. The matrix is a table with a written source,
and every row here names the row it comes from.
"""

from __future__ import annotations

import pytest

from app.models.enums import DeviationState, MedicationStatusToday, TrendDirection
from app.services import language
from app.services.insight import (
    InsightFeatures,
    PriorityAction,
    ResultState,
    evaluate,
)


def sensor(**overrides) -> InsightFeatures:
    base = {
        "sensor_mode": True,
        "trend_direction": TrendDirection.STABLE,
        "deviation_state": DeviationState.NONE,
        "reference_systolic": 128,
        "reference_diastolic": 82,
        "hr_near_resting": True,
        "precondition_standard": True,
    }
    return InsightFeatures(**{**base, **overrides})


def bp_only(**overrides) -> InsightFeatures:
    base = {"sensor_mode": False, "confirmed_systolic": 128, "confirmed_diastolic": 82}
    return InsightFeatures(**{**base, **overrides})


class TestSensorEligible:
    """Section 24.1."""

    def test_stable_under_threshold_offers_a_preventive_step(self):
        # The everyday result. The action moved from `continue_monitoring` to
        # `preventive_recommendation` with the intervention engine; the state did not.
        result = evaluate(sensor())

        assert result.result_state is ResultState.WITHIN_PATTERN
        assert result.priority_action_code is PriorityAction.PREVENTIVE_RECOMMENDATION

    def test_a_reference_above_threshold_outranks_a_stable_trend(self):
        # Was: `within_pattern` with a `reference_above_threshold` context chip beside it. The
        # cuff figure is now checked first and decides the row on its own, so a stable trend on
        # top of a 148/94 reference reports the reference and routes to follow-up rather than
        # reporting the trend and mentioning the reference. An escalation, not a relaxation.
        result = evaluate(sensor(reference_systolic=148, reference_diastolic=94))

        assert result.result_state is ResultState.BP_ABOVE_THRESHOLD
        assert result.priority_action_code is PriorityAction.FOLLOW_UP_PATHWAY
        assert result.reference_systolic == 148

    def test_single_change_at_rest_repeats_later(self):
        # "First change; persistence not established" -> repeat later.
        result = evaluate(
            sensor(trend_direction=TrendDirection.INCREASE, deviation_state=DeviationState.POSSIBLE)
        )

        assert result.result_state is ResultState.SINGLE_CHANGE
        assert result.priority_action_code is PriorityAction.REPEAT_LATER

    def test_single_change_with_high_hr_rests_first(self):
        # "Less comparable" -> rest + repeat.
        result = evaluate(
            sensor(trend_direction=TrendDirection.INCREASE, hr_near_resting=False)
        )

        assert result.result_state is ResultState.SINGLE_CHANGE
        assert result.priority_action_code is PriorityAction.REST_AND_REPEAT
        assert "hr_above_resting" in result.context_codes

    def test_single_change_with_non_standard_precondition_rests_first(self):
        # "Potential confounding" -> repeat. The single-change row no longer distinguishes the two
        # reasons a check is less comparable: a raised heart rate and a non-standard precondition
        # both produce `rest_and_repeat`, where the precondition case used to get its own
        # `standardize_and_repeat`. The context chip still names which one it was.
        result = evaluate(
            sensor(trend_direction=TrendDirection.DECREASE, precondition_standard=False)
        )

        assert result.result_state is ResultState.SINGLE_CHANGE
        assert result.priority_action_code is PriorityAction.REST_AND_REPEAT
        assert "non_standard_precondition" in result.context_codes

    def test_persistent_at_rest_asks_for_a_cuff(self):
        # "Persistent BP-related change" -> confirm with fresh cuff BP. The only thing that can
        # turn a trend into a pressure is a cuff.
        result = evaluate(
            sensor(
                trend_direction=TrendDirection.INCREASE,
                deviation_state=DeviationState.PERSISTENT,
            )
        )

        assert result.result_state is ResultState.PERSISTENT_CHANGE
        assert result.priority_action_code is PriorityAction.CONFIRM_WITH_CUFF

    def test_persistent_with_high_hr_standardizes_before_interpreting(self):
        # "Need cleaner measurement before strong interpretation."
        result = evaluate(
            sensor(
                trend_direction=TrendDirection.INCREASE,
                deviation_state=DeviationState.PERSISTENT,
                hr_near_resting=False,
            )
        )

        assert result.result_state is ResultState.PERSISTENT_CHANGE
        assert result.priority_action_code is PriorityAction.STANDARDIZE_AND_REPEAT

    def test_persistent_plus_new_cuff_below_threshold_still_asks_for_a_cuff(self):
        # Was: the fresh cuff settled it, so `continue_monitoring`, and the reported reference
        # became the new 132. Neither holds now — a persistent change asks for a cuff whatever
        # confirming reading already exists, and the reported reference stays the calibration
        # anchor rather than the reading just taken.
        #
        # **Worth a second look.** Asking a patient to confirm with a cuff when they have just
        # supplied one reads as the app not having noticed, and the 128 shown beside it is not the
        # most recent thing known about them. Recorded rather than corrected: see docs/decisions.md.
        result = evaluate(
            sensor(
                trend_direction=TrendDirection.INCREASE,
                deviation_state=DeviationState.PERSISTENT,
                confirmed_systolic=132,
                confirmed_diastolic=84,
            )
        )

        assert result.result_state is ResultState.PERSISTENT_CHANGE
        assert result.priority_action_code is PriorityAction.CONFIRM_WITH_CUFF
        assert result.reference_systolic == 128

    def test_persistent_plus_new_cuff_above_threshold_is_the_follow_up_row(self):
        # The strongest row in the table: an actual BP above threshold *and* a persistent pattern.
        # The action is unchanged; the state is now `bp_above_threshold` rather than
        # `persistent_change`, because the cuff figure is checked first and names what is driving
        # the escalation. The measured reading is what gets reported back, not the anchor.
        result = evaluate(
            sensor(
                trend_direction=TrendDirection.INCREASE,
                deviation_state=DeviationState.PERSISTENT,
                confirmed_systolic=152,
                confirmed_diastolic=96,
            )
        )

        assert result.result_state is ResultState.BP_ABOVE_THRESHOLD
        assert result.priority_action_code is PriorityAction.FOLLOW_UP_PATHWAY
        assert result.reference_systolic == 152

    def test_comparability_is_checked_before_the_trend_is_interpreted(self):
        # A change measured on someone who was not at rest is not evidence of a change in them.
        at_rest = evaluate(sensor(trend_direction=TrendDirection.INCREASE))
        not_at_rest = evaluate(
            sensor(trend_direction=TrendDirection.INCREASE, hr_near_resting=False)
        )

        assert at_rest.priority_action_code is not not_at_rest.priority_action_code


class TestNotEligible:
    """Section 24.2."""

    def test_confirmed_below_threshold_continues_routine_monitoring(self):
        result = evaluate(bp_only())

        assert result.result_state is ResultState.BP_WITHIN_THRESHOLD
        assert result.priority_action_code is PriorityAction.CONTINUE_MONITORING

    def test_confirmed_above_threshold_stores_and_repeats_without_diagnosing(self):
        # "Store, repeat/monitor, do not diagnose."
        result = evaluate(bp_only(confirmed_systolic=152, confirmed_diastolic=96))

        assert result.result_state is ResultState.BP_ABOVE_THRESHOLD
        assert result.priority_action_code is PriorityAction.REPEAT_LATER

    def test_either_number_over_threshold_counts(self):
        systolic_only = evaluate(bp_only(confirmed_systolic=142, confirmed_diastolic=80))
        diastolic_only = evaluate(bp_only(confirmed_systolic=120, confirmed_diastolic=92))

        assert systolic_only.result_state is ResultState.BP_ABOVE_THRESHOLD
        assert diastolic_only.result_state is ResultState.BP_ABOVE_THRESHOLD

    def test_the_threshold_boundary_is_inclusive(self):
        assert evaluate(bp_only(confirmed_systolic=140, confirmed_diastolic=89)).result_state is (
            ResultState.BP_ABOVE_THRESHOLD
        )
        assert evaluate(bp_only(confirmed_systolic=139, confirmed_diastolic=89)).result_state is (
            ResultState.BP_WITHIN_THRESHOLD
        )

    def test_no_reading_at_all_asks_for_one_rather_than_guessing(self):
        result = evaluate(
            InsightFeatures(sensor_mode=False, confirmed_systolic=None, confirmed_diastolic=None)
        )

        assert result.result_state is ResultState.NO_RESULT
        assert result.priority_action_code is PriorityAction.SET_BP_REFERENCE


class TestContextIsContextNotCause:
    def test_a_missed_dose_is_surfaced_and_never_acted_on(self):
        # Section 24 says "no dose change advice" in its own row. The code appears; the action
        # does not become a medication instruction. The action itself moved to
        # `preventive_recommendation` with the intervention engine, which is still not a dose
        # instruction — `test_no_action_wording_anywhere_is_a_dose_instruction` holds the wording.
        result = evaluate(sensor(medication_status=MedicationStatusToday.MISSED_OR_LATE))

        assert "medication_missed" in result.context_codes
        assert result.priority_action_code is PriorityAction.PREVENTIVE_RECOMMENDATION

    def test_a_missed_dose_on_a_persistent_change_routes_to_intervention(self):
        # **The branch this file previously had no test for, and the one worth arguing about.**
        #
        # A persistent change is escalated with `confirm_with_cuff` — except when it carries a
        # lifestyle context or a missed dose, which now diverts it to
        # `personalized_intervention`. So the case with *more* going on gets advice where the
        # plainer case gets a cuff.
        #
        # Invariant 7 asks for a cuff or clinical contact when the picture is ambiguous, and
        # invariant 6 forbids advising on medication — a missed dose steering the branch runs at
        # both. Raised with the product owner and left in place by their decision; this test
        # exists so the behaviour is visible rather than merely present, and the wording behind
        # `personalized_intervention` keeps the cuff in the sentence the patient reads.
        result = evaluate(
            sensor(
                trend_direction=TrendDirection.INCREASE,
                deviation_state=DeviationState.PERSISTENT,
                medication_status=MedicationStatusToday.MISSED_OR_LATE,
            )
        )

        assert result.result_state is ResultState.PERSISTENT_CHANGE
        assert result.priority_action_code is PriorityAction.PERSONALIZED_INTERVENTION
        assert "medication_missed" in result.context_codes

    def test_sleep_or_stress_on_a_persistent_change_does_the_same(self):
        # Same branch, reached by the other two contexts, so the divergence is not read as
        # specific to medication.
        for context in ({"sleep_less_than_usual": True}, {"stress_higher_than_usual": True}):
            result = evaluate(
                sensor(
                    trend_direction=TrendDirection.INCREASE,
                    deviation_state=DeviationState.PERSISTENT,
                    **context,
                )
            )
            assert result.priority_action_code is PriorityAction.PERSONALIZED_INTERVENTION

    def test_no_action_wording_anywhere_is_a_dose_instruction(self):
        for wording in language.PRIORITY_ACTION_WORDING.values():
            lowered = wording.lower()
            for forbidden in ("dose", "take your", "stop taking", "increase", "reduce your medic"):
                assert forbidden not in lowered, wording

    def test_sleep_and_stress_are_carried_without_changing_the_verdict(self):
        plain = evaluate(sensor())
        with_context = evaluate(
            sensor(sleep_less_than_usual=True, stress_higher_than_usual=True)
        )

        assert with_context.result_state is plain.result_state
        assert with_context.priority_action_code is plain.priority_action_code
        assert {"less_sleep", "higher_stress"} <= set(with_context.context_codes)

    def test_the_disclaimer_denies_causation(self):
        assert "does not assume they caused" in language.CONTEXT_DISCLAIMER


class TestRefusalsAndInvariants:
    def test_a_rejected_session_produces_no_result_about_the_patient(self):
        result = evaluate(sensor(session_rejected=True))

        assert result.result_state is ResultState.NO_RESULT
        # Not "stable": a capture that produced nothing says nothing.
        assert result.result_state is not ResultState.WITHIN_PATTERN

    def test_a_sensor_insight_never_carries_a_pressure_it_was_not_given(self):
        # Invariant 1: mmHg comes from a cuff reading or not at all.
        result = evaluate(
            InsightFeatures(
                sensor_mode=True,
                trend_direction=TrendDirection.INCREASE,
                deviation_state=DeviationState.PERSISTENT,
            )
        )

        assert result.reference_systolic is None
        assert result.reference_diastolic is None

    def test_the_engine_is_pure(self):
        features = sensor(trend_direction=TrendDirection.INCREASE)
        verdicts = {evaluate(features).priority_action_code for _ in range(5)}

        assert len(verdicts) == 1

    @pytest.mark.invariant
    def test_no_wording_diagnoses_or_reassures(self):
        every = (
            list(language.RESULT_STATE_WORDING.values())
            + list(language.PRIORITY_ACTION_WORDING.values())
            + list(language.CONTEXT_CODE_WORDING.values())
            + [language.CONTEXT_DISCLAIMER, language.INSIGHT_NOTICE]
        )

        for text in every:
            lowered = text.lower()
            for forbidden in (
                "hypertension",
                "diagnos" if "not a diagnosis" not in lowered else "\0",
                "you are fine",
                "nothing to worry",
                "normal for you",
                "healthy",
            ):
                assert forbidden not in lowered, text

    def test_every_code_the_engine_can_emit_has_wording(self):
        # A code with no sentence would render as a blank space on a patient's screen.
        for state in ResultState:
            assert state.value in language.RESULT_STATE_WORDING
        for action in PriorityAction:
            assert action.value in language.PRIORITY_ACTION_WORDING
