"""Every user-facing string the API emits, in one place.

Two invariants meet here.

**Invariant 6** — the system never diagnoses and never advises on medication. Keeping the
strings in one module means ``test_no_diagnostic_or_medication_advice_language`` can enumerate
all of them and check them against the deny-list, rather than hoping a reviewer notices a
sentence added inline in a route six months from now.

**Invariant 9** — synthetic data is unmistakably labelled. The synthetic notice lives here too.

If you add a user-facing string anywhere in ``app/``, add it here instead and import it.
"""

from __future__ import annotations

import re

from app.models.enums import RejectionReason, TrendDirection

# --------------------------------------------------------------------------- badges

#: BUILD_SPEC 5.2 — the estimate badge is not dismissible and travels with the record from the
#: API, so a client cannot render an estimate without it.
ESTIMATE_BADGE = "ESTIMATE — NOT A BLOOD-PRESSURE READING"
CUFF_BADGE = "CONFIRMED — UPPER-ARM CUFF"
REJECTED_BADGE = "SESSION NOT USABLE"
SYNTHETIC_BADGE = "SYNTHETIC DEMONSTRATION DATA — NOT A REAL MEASUREMENT"

#: Device profiles need their own wording. Invariant 9 singles them out — "never invent device
#: benchmark results" — because a seeded profile is the one synthetic record that reads as a
#: hardware benchmark: "204.8 Hz" looks like something somebody measured on a bench. The generic
#: badge says the row is not a measurement *from a person*, which is the wrong reassurance here.
#: This says what the numbers actually are.
SYNTHETIC_DEVICE_PROFILE_NOTICE = (
    "SYNTHETIC SEED DATA — ILLUSTRATIVE OF UI STATES, NOT MEASURED PERFORMANCE"
)

# --------------------------------------------------------------------------- estimate wording

#: BUILD_SPEC 5.2 gives this wording. Note what it does *not* say: nothing about whether the
#: reading is good, safe or concerning. It describes where the number sits relative to this
#: patient's own baseline and stops there.
DIRECTION_WORDING: dict[TrendDirection, str] = {
    TrendDirection.STABLE: "within your usual range",
    TrendDirection.INCREASE: "higher than your usual range",
    TrendDirection.DECREASE: "lower than your usual range",
}

# --------------------------------------------------------------------------- next actions

ACTION_NONE = "No action needed from this session. Keep to your usual measurement schedule."
ACTION_REPEAT_SUGGESTED = (
    "This session read outside your usual range. A single session is not enough to act on. "
    "Take another spot check within the next day so the pattern can be seen."
)
#: Invariant 7 — where the picture is ambiguous the answer is a cuff reading, not an estimate.
ACTION_CUFF_REQUESTED = (
    "Repeat sessions have read outside your usual range. Take an upper-arm cuff reading and "
    "enter it, so there is a confirmed measurement on the record."
)
ACTION_CUFF_REQUESTED_NO_CALIBRATION = (
    "This device has no active calibration, so no trend can be shown. Take an upper-arm cuff "
    "reading and enter it to set up a baseline."
)
ACTION_CUFF_REQUESTED_SESSION_UNUSABLE = (
    "This session could not be used. Take an upper-arm cuff reading if one is due, and try "
    "another spot check when you can."
)
#: Invariant 8. The handset shows this locally the moment a red flag is selected; this copy of
#: it is a record of what was shown, not the trigger for showing it.
ACTION_SEEK_EMERGENCY_CARE = (
    "Seek emergency care now. Call your local emergency number or go to an emergency "
    "department. Do not wait for a measurement."
)

#: Server-side contraindication gate. The handset holds a copy of the first sentence in
#: ``context_intake.dart``; this is the one the API returns.
#:
#: Invariant 6 applies here as everywhere: it names the limitation and refers on. It does not say
#: what the reading might have shown, does not estimate risk, and does not reassure.
CONTRAINDICATED_PREGNANCY = (
    "Method unvalidated in pregnancy. Please consult your doctor. Tera does not produce trend "
    "estimates while pregnancy is recorded on this account."
)

# --------------------------------------------------------------------------- rejection wording

#: Plain-language rejection text for the patient (BUILD_SPEC 5.4). Every one of these describes
#: what the *device* could not do. None of them describes the patient.
REJECTION_WORDING: dict[RejectionReason, str] = {
    RejectionReason.POOR_SIGNAL_QUALITY: (
        "The pulse signal from the camera was too weak to use. Cover the lens fully with your "
        "fingertip and keep steady pressure."
    ),
    RejectionReason.INSUFFICIENT_BEATS: (
        "Not enough usable heartbeats were captured in this session. Try again when you can sit "
        "still for the full recording."
    ),
    RejectionReason.EXCESSIVE_MOTION: (
        "There was too much movement during the recording. Rest your arm and stay still for the "
        "whole session."
    ),
    RejectionReason.POSTURE_UNSTABLE: (
        "Your position shifted during the recording. Stay in one position from start to finish."
    ),
    RejectionReason.TORCH_UNAVAILABLE: (
        "The camera light could not be turned on, so the pulse signal could not be recorded."
    ),
    RejectionReason.SENSOR_RATE_BELOW_QUALIFIED: (
        "The phone's sensors ran slower than this device was set up for, so the timing was not "
        "precise enough to use. Closing other apps may help."
    ),
    RejectionReason.CLOCK_UNSTABLE: (
        "The phone's two internal clocks drifted apart during the recording, so the timing could "
        "not be trusted."
    ),
    RejectionReason.USER_ABORTED: "The session was stopped before it finished.",
    RejectionReason.RED_FLAG_REPORTED: (
        "The session was stopped because you reported a symptom that needs urgent attention."
    ),
    RejectionReason.IMPLAUSIBLE_PAYLOAD: (
        "The recording did not pass the checks on the server and was not used."
    ),
    RejectionReason.NO_ACTIVE_CALIBRATION: (
        "This phone has no active baseline yet, so the recording was kept but no trend could be "
        "worked out from it."
    ),
    # Says plainly that the app is unfinished, rather than implying the recording was at fault.
    # Anything vaguer would read as a signal problem, which is the one thing this value exists to
    # be distinguishable from.
    RejectionReason.SIGNAL_PROCESSING_UNAVAILABLE: (
        "This version of the app records both signals but cannot yet work out a result from "
        "them. The recording was kept, and nothing was estimated from it."
    ),
}

# --------------------------------------------------------------------------- notices

CONFIDENCE_NOTICE = (
    "Confidence reflects how much usable signal this session produced. It is a signal-quality "
    "indicator, not a measure of accuracy."
)
MAGNITUDE_NOTICE = (
    "Magnitude is measured in standard deviations of your own baseline. It is not a blood "
    "pressure and does not convert to one."
)
SYNTHETIC_NOTICE = (
    "This record was generated for demonstration. It is not a measurement from a person."
)

# --------------------------------------------------------------------------- the deny-list

#: Phrases that would make the system diagnose, advise on medication, or reassure. Matched
#: case-insensitively as whole words or phrases against every string above.
#:
#: The list is not a substitute for judgement — it catches the obvious failures. The real
#: protection is that all user-facing copy lives in this module where it can be reviewed.
FORBIDDEN_PATTERNS: tuple[str, ...] = (
    # Diagnosis.
    r"\bdiagnos\w*",
    r"\byou (?:have|are suffering|may have)\b",
    r"\bhypertensive\b",
    r"\byour (?:condition|disease)\b",
    # Medication advice.
    r"\b(?:increase|decrease|reduce|adjust|change|stop|start|skip|double)\s+(?:your\s+)?(?:dose|dosage|medication|tablets?|treatment)\b",
    r"\bmedication (?:change|adjustment)\b",
    r"\btalk to your doctor about (?:changing|adjusting|stopping)\b",
    # Clinical reassurance (invariant 6) — the system must not tell anyone they are fine.
    r"\byour blood pressure is (?:normal|fine|good|healthy|controlled|under control)\b",
    r"\b(?:no cause for concern|nothing to worry about|don'?t worry|you'?re fine|all clear)\b",
    r"\b(?:reassur\w*)",
    r"\b(?:normal|healthy) (?:reading|result|range|blood pressure)\b",
    r"\b(?:safe|dangerous) (?:level|reading|range)\b",
    # Implying the estimate is a measurement (invariant 1 expressed in words).
    r"\bestimated (?:blood pressure|pressure|systolic|diastolic)\b",
    r"\byour (?:estimated|predicted) (?:blood pressure|pressure)\b",
)

_COMPILED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in FORBIDDEN_PATTERNS)


def find_forbidden_language(text: str) -> list[str]:
    """Return every deny-listed phrase found in ``text``."""
    return [match.group(0) for pattern in _COMPILED for match in pattern.finditer(text)]


def all_user_facing_strings() -> dict[str, str]:
    """Every user-facing string in this module, keyed by a readable name.

    ``test_no_diagnostic_or_medication_advice_language`` iterates this, so a new string added to
    the module is covered automatically.
    """
    strings: dict[str, str] = {
        "ESTIMATE_BADGE": ESTIMATE_BADGE,
        "CUFF_BADGE": CUFF_BADGE,
        "REJECTED_BADGE": REJECTED_BADGE,
        "SYNTHETIC_BADGE": SYNTHETIC_BADGE,
        "ACTION_NONE": ACTION_NONE,
        "ACTION_REPEAT_SUGGESTED": ACTION_REPEAT_SUGGESTED,
        "ACTION_CUFF_REQUESTED": ACTION_CUFF_REQUESTED,
        "ACTION_CUFF_REQUESTED_NO_CALIBRATION": ACTION_CUFF_REQUESTED_NO_CALIBRATION,
        "ACTION_CUFF_REQUESTED_SESSION_UNUSABLE": ACTION_CUFF_REQUESTED_SESSION_UNUSABLE,
        "ACTION_SEEK_EMERGENCY_CARE": ACTION_SEEK_EMERGENCY_CARE,
        "CONTRAINDICATED_PREGNANCY": CONTRAINDICATED_PREGNANCY,
        "CONFIDENCE_NOTICE": CONFIDENCE_NOTICE,
        "MAGNITUDE_NOTICE": MAGNITUDE_NOTICE,
        "SYNTHETIC_NOTICE": SYNTHETIC_NOTICE,
    }
    strings.update(
        {f"DIRECTION_WORDING[{key.value}]": value for key, value in DIRECTION_WORDING.items()}
    )
    strings.update(
        {f"REJECTION_WORDING[{key.value}]": value for key, value in REJECTION_WORDING.items()}
    )
    return strings
