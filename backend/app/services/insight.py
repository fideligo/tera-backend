"""The intervention rule engine (PM spec section 24).

**Deterministic code, not model output.** The spec says so and invariant 6 requires it: every
sentence a patient reads about their own measurement comes from a table of rules that can be read,
argued with, and tested. There is no generation step here.

The engine returns a `result_state` and a `priority_action_code`. The *wording* for those codes
lives in `language.py` with the rest of the patient-facing copy, so the decision and the sentence
can be reviewed separately — the whole reason the spec separates `deterministicRuleEngine` from
`languageLayer`.

# What it will not do

- **No diagnosis, ever.** Nothing here concludes anything about a disease.
- **No medication advice.** A missed dose is surfaced as context and never turned into "take it"
  or "skip it". Section 24 says "no dose change advice" in two separate rows; it is a rule.
- **No causal claim from context.** Poor sleep and higher stress are reported alongside a result,
  never as its cause. The spec's own disclaimer says this and the copy repeats it.
- **No mmHg from a sensor trend.** Invariant 1. A pressure value appears only when a cuff reading
  supplied it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.models.enums import DeviationState, MedicationStatusToday, TrendDirection


class ResultState(str, Enum):
    """The hero result. Section 23.1's three sensor states, plus the BP-only pair."""

    # Sensor (eligible device).
    WITHIN_PATTERN = "within_pattern"
    SINGLE_CHANGE = "single_change"
    PERSISTENT_CHANGE = "persistent_change"

    # BP-only (not eligible).
    BP_WITHIN_THRESHOLD = "bp_within_threshold"
    BP_ABOVE_THRESHOLD = "bp_above_threshold"

    #: Nothing usable was produced. Not a result about the patient.
    NO_RESULT = "no_result"


class PriorityAction(str, Enum):
    """Section 23.4's "Your Next Best Step". One per insight, the most important one."""

    CONTINUE_MONITORING = "continue_monitoring"
    REPEAT_LATER = "repeat_later"
    REST_AND_REPEAT = "rest_and_repeat"
    STANDARDIZE_AND_REPEAT = "standardize_and_repeat"
    CONFIRM_WITH_CUFF = "confirm_with_cuff"
    FOLLOW_UP_PATHWAY = "follow_up_pathway"
    SET_BP_REFERENCE = "set_bp_reference"
    PREVENTIVE_RECOMMENDATION = "preventive_recommendation"
    PERSONALIZED_INTERVENTION = "personalized_intervention"


#: Section 24's threshold, and the only number in this file. It is a *monitoring* threshold used to
#: describe a cuff reading the patient already has — never a diagnosis, and never applied to a
#: sensor trend, which has no mmHg to compare (invariant 1).
THRESHOLD_SYSTOLIC = 140
THRESHOLD_DIASTOLIC = 90


@dataclass(frozen=True)
class InsightFeatures:
    """Everything the matrix reads. Assembled by the caller; this module does no IO."""

    #: True when the device is sensor-eligible and produced a trend.
    sensor_mode: bool

    # --- sensor path ---
    trend_direction: TrendDirection | None = None
    deviation_state: DeviationState | None = None

    # --- cuff reference / confirmed reading ---
    reference_systolic: int | None = None
    reference_diastolic: int | None = None
    #: A cuff reading taken as part of *this* check, which supersedes the reference for wording.
    confirmed_systolic: int | None = None
    confirmed_diastolic: int | None = None

    # --- comparability ---
    hr_near_resting: bool = True
    precondition_standard: bool = True

    # --- context ---
    medication_status: MedicationStatusToday | None = None
    sleep_less_than_usual: bool = False
    stress_higher_than_usual: bool = False

    #: Set when the session produced nothing usable.
    session_rejected: bool = False


@dataclass(frozen=True)
class Insight:
    """The engine's verdict. Codes only — wording is applied downstream."""

    result_state: ResultState
    priority_action_code: PriorityAction

    #: Secondary observations, in the order the matrix produced them. Each is a code, and each is
    #: context rather than cause.
    context_codes: list[str] = field(default_factory=list)

    #: The reference or confirmed reading, when one exists. Never derived from a sensor trend.
    reference_systolic: int | None = None
    reference_diastolic: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "result_state": self.result_state.value,
            "priority_action_code": self.priority_action_code.value,
            "context_codes": list(self.context_codes),
            "reference_systolic": self.reference_systolic,
            "reference_diastolic": self.reference_diastolic,
        }


def _above_threshold(systolic: int | None, diastolic: int | None) -> bool | None:
    """None when there is no reading to judge. Absence is not "below"."""
    if systolic is None or diastolic is None:
        return None
    return systolic >= THRESHOLD_SYSTOLIC or diastolic >= THRESHOLD_DIASTOLIC


def _context_codes(f: InsightFeatures) -> list[str]:
    """Secondary observations, each shown as context and never as a cause.

    Section 24 lists these as their own rows precisely so they accompany a result rather than
    change it.
    """
    codes: list[str] = []
    if f.medication_status is MedicationStatusToday.MISSED_OR_LATE:
        # "Medication context is relevant" — and, in the same row, "no dose change advice".
        codes.append("medication_missed")
    if f.sleep_less_than_usual:
        codes.append("less_sleep")
    if f.stress_higher_than_usual:
        codes.append("higher_stress")
    if not f.hr_near_resting:
        codes.append("hr_above_resting")
    if not f.precondition_standard:
        codes.append("non_standard_precondition")
    return codes


def evaluate(f: InsightFeatures) -> Insight:
    """Section 24, as a decision. Pure: same features in, same verdict out."""
    context = _context_codes(f)

    # A session that produced nothing says nothing about the patient. It is not a "stable" result.
    if f.session_rejected:
        return Insight(
            result_state=ResultState.NO_RESULT,
            priority_action_code=PriorityAction.REPEAT_LATER,
            context_codes=context,
            reference_systolic=f.reference_systolic,
            reference_diastolic=f.reference_diastolic,
        )

    if not f.sensor_mode:
        return _evaluate_bp_only(f, context)
    return _evaluate_sensor(f, context)


def _evaluate_bp_only(f: InsightFeatures, context: list[str]) -> Insight:
    """Section 24.2. The confirmed reading is the measurement."""
    systolic = f.confirmed_systolic if f.confirmed_systolic is not None else f.reference_systolic
    diastolic = (
        f.confirmed_diastolic if f.confirmed_diastolic is not None else f.reference_diastolic
    )
    above = _above_threshold(systolic, diastolic)

    if above is None:
        return Insight(
            result_state=ResultState.NO_RESULT,
            priority_action_code=PriorityAction.SET_BP_REFERENCE,
            context_codes=context,
        )

    if above:
        # "Store, repeat/monitor, do not diagnose" for a first elevated reading; follow-up becomes
        # more relevant when they repeat. Repetition is not modelled yet, so the conservative
        # single-reading row is used and the wording does not escalate on one number.
        return Insight(
            result_state=ResultState.BP_ABOVE_THRESHOLD,
            priority_action_code=PriorityAction.REPEAT_LATER,
            context_codes=context,
            reference_systolic=systolic,
            reference_diastolic=diastolic,
        )

    return Insight(
        result_state=ResultState.BP_WITHIN_THRESHOLD,
        priority_action_code=PriorityAction.CONTINUE_MONITORING,
        context_codes=context,
        reference_systolic=systolic,
        reference_diastolic=diastolic,
    )


def _evaluate_sensor(f: InsightFeatures, context: list[str]) -> Insight:
    """Section 24.1.

    Comparability is checked **before** the trend is interpreted, in both the single-change and
    persistent rows. A change measured on a patient who was not at rest is not evidence of a change
    in the patient, and the matrix says so twice.
    """
    comparable = f.hr_near_resting and f.precondition_standard
    above_reference = _above_threshold(f.reference_systolic, f.reference_diastolic)
    confirmed_above = _above_threshold(f.confirmed_systolic, f.confirmed_diastolic)

    # BP cuff >= 140/90 -> actual BP is primary concern; direct to medical follow-up (InaSH rules).
    if confirmed_above or (confirmed_above is None and above_reference):
        return Insight(
            result_state=ResultState.BP_ABOVE_THRESHOLD,
            priority_action_code=PriorityAction.FOLLOW_UP_PATHWAY,
            context_codes=context,
            reference_systolic=f.confirmed_systolic if confirmed_above is not None else f.reference_systolic,
            reference_diastolic=f.confirmed_diastolic if confirmed_above is not None else f.reference_diastolic,
        )

    if f.deviation_state is DeviationState.PERSISTENT:
        has_lifestyle = f.sleep_less_than_usual or f.stress_higher_than_usual or f.medication_status == MedicationStatusToday.MISSED_OR_LATE
        if has_lifestyle:
            action = PriorityAction.PERSONALIZED_INTERVENTION
        elif comparable:
            action = PriorityAction.CONFIRM_WITH_CUFF
        else:
            action = PriorityAction.STANDARDIZE_AND_REPEAT
        
        return Insight(
            result_state=ResultState.PERSISTENT_CHANGE,
            priority_action_code=action,
            context_codes=context,
            reference_systolic=f.reference_systolic,
            reference_diastolic=f.reference_diastolic,
        )

    if f.trend_direction is not None and f.trend_direction is not TrendDirection.STABLE:
        if not f.hr_near_resting or not f.precondition_standard:
            action = PriorityAction.REST_AND_REPEAT
        else:
            action = PriorityAction.REPEAT_LATER
            
        return Insight(
            result_state=ResultState.SINGLE_CHANGE,
            priority_action_code=action,
            context_codes=context,
            reference_systolic=f.reference_systolic,
            reference_diastolic=f.reference_diastolic,
        )

    # Stable -> reassurance + continue monitoring + preventive recommendation.
    return Insight(
        result_state=ResultState.WITHIN_PATTERN,
        priority_action_code=PriorityAction.PREVENTIVE_RECOMMENDATION,
        context_codes=context,
        reference_systolic=f.reference_systolic,
        reference_diastolic=f.reference_diastolic,
    )
