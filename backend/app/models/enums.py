"""Enumerations shared by the models, the Pydantic schemas and the services.

These are Postgres native enum types. Widening one is a migration, which is deliberate: the set
of rejection reasons and the set of trend directions are both places where a careless addition
could weaken an invariant.
"""

from __future__ import annotations

from enum import Enum


class UserRole(str, Enum):
    """Role claim carried in the JWT (BUILD_SPEC 4.5)."""

    PATIENT = "patient"
    CLINICIAN = "clinician"
    ADMIN = "admin"


class QualifiedStatus(str, Enum):
    """Device eligibility verdict returned by POST /v1/device-profiles."""

    QUALIFIED = "qualified"
    PROVISIONAL = "provisional"
    NOT_QUALIFIED = "not_qualified"


class TimestampSource(str, Enum):
    """Android SENSOR_INFO_TIMESTAMP_SOURCE.

    ``realtime`` means camera and sensor timestamps share a time base; ``unknown`` means they
    must be aligned through an inferred offset, which is the error PTT is most sensitive to.
    """

    UNKNOWN = "unknown"
    REALTIME = "realtime"


class CameraHardwareLevel(str, Enum):
    """Android INFO_SUPPORTED_HARDWARE_LEVEL."""

    LEGACY = "legacy"
    LIMITED = "limited"
    FULL = "full"
    LEVEL_3 = "level_3"
    EXTERNAL = "external"


class CalibrationStatus(str, Enum):
    """Invariant 4 — at most one active calibration per patient per device."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class SessionStatus(str, Enum):
    """Invariant 3 — rejected sessions are retained, never discarded."""

    COMPLETED = "completed"
    REJECTED = "rejected"


class RejectionReason(str, Enum):
    """Why a session did not yield a usable measurement.

    BUILD_SPEC does not enumerate these; the set below covers the device-side quality gate and
    the server-side plausibility gate (BUILD_SPEC 4.4). Every value is a *system* condition —
    none of them describes a physiological finding, because a rejected session says nothing
    about the patient (invariant 6).
    """

    # Device-side quality gate outcomes.
    POOR_SIGNAL_QUALITY = "poor_signal_quality"
    INSUFFICIENT_BEATS = "insufficient_beats"
    EXCESSIVE_MOTION = "excessive_motion"
    POSTURE_UNSTABLE = "posture_unstable"
    TORCH_UNAVAILABLE = "torch_unavailable"
    SENSOR_RATE_BELOW_QUALIFIED = "sensor_rate_below_qualified"
    CLOCK_UNSTABLE = "clock_unstable"
    USER_ABORTED = "user_aborted"
    # Invariant 8 — a red flag terminates the session before any measurement is offered.
    RED_FLAG_REPORTED = "red_flag_reported"
    # Server-side plausibility gate (defence in depth).
    IMPLAUSIBLE_PAYLOAD = "implausible_payload"
    # Invariant 7 — no calibration in force means no estimate, not a guess.
    NO_ACTIVE_CALIBRATION = "no_active_calibration"
    # Build state, not signal quality. The handset captured both streams correctly but the
    # signal chain that derives per-beat intervals is not implemented in that build. Kept
    # distinct from POOR_SIGNAL_QUALITY on purpose: collapsing the two would make an unfinished
    # component indistinguishable from a working one that happened to reject, in the clinical
    # record itself. Sessions carrying it are still stored and still surfaced (invariant 3).
    SIGNAL_PROCESSING_UNAVAILABLE = "signal_processing_unavailable"


class Posture(str, Enum):
    """Posture during capture. Recorded because posture shifts PTT independently of pressure."""

    SEATED = "seated"
    SUPINE = "supine"
    SEMI_RECUMBENT = "semi_recumbent"
    STANDING = "standing"


class TrendDirection(str, Enum):
    """Invariant 1 — a direction, never a pressure value.

    ``increase`` means PTT shortened relative to baseline. Shorter transit time implies a faster
    pulse wave, which implies a stiffer artery, which is associated with higher pressure. It is
    an association in the patient's own baseline units, not a measurement of pressure.
    """

    STABLE = "stable"
    INCREASE = "increase"
    DECREASE = "decrease"


class DeviationState(str, Enum):
    """BUILD_SPEC 4.3 — a single deviating session never triggers a cuff request."""

    NONE = "none"
    POSSIBLE = "possible"
    PERSISTENT = "persistent"


class CuffSource(str, Enum):
    """How a cuff reading reached the record.

    ``photograph`` exists in the schema for completeness but the API rejects it: seven-segment
    OCR is out of scope (BUILD_SPEC 8), and accepting the value would imply a capability that
    does not exist.
    """

    MANUAL_ENTRY = "manual_entry"
    PHOTOGRAPH = "photograph"


class EventType(str, Enum):
    """Discriminator for POST /v1/events."""

    MEDICATION = "medication"
    SYMPTOM = "symptom"
    RED_FLAG = "red_flag"


class AuditAction(str, Enum):
    """Actions written to the append-only audit log."""

    DEVICE_PROFILE_SUBMITTED = "device_profile_submitted"
    NONCE_ISSUED = "nonce_issued"
    SESSION_SUBMITTED = "session_submitted"
    SESSION_DUPLICATE_REPLAYED = "session_duplicate_replayed"
    CUFF_READING_RECORDED = "cuff_reading_recorded"
    PATIENT_CONTEXT_RECORDED = "patient_context_recorded"
    PHR_PROFILE_UPDATED = "phr_profile_updated"
    SESSION_CONTEXT_RECORDED = "session_context_recorded"
    CHECK_SESSION_CREATED = "check_session_created"
    CHECK_SESSION_ADVANCED = "check_session_advanced"
    PRECONDITIONS_RECORDED = "preconditions_recorded"
    MEDICATIONS_UPDATED = "medications_updated"
    BP_REFERENCE_ACTIVATED = "bp_reference_activated"
    CALIBRATION_ESTABLISHED = "calibration_established"
    CALIBRATION_SUPERSEDED = "calibration_superseded"
    EVENT_RECORDED = "event_recorded"
    TIMELINE_VIEWED = "timeline_viewed"
    SUMMARY_GENERATED = "summary_generated"
    AUTH_TOKEN_ISSUED = "auth_token_issued"
    AUTH_LOGIN_FAILED = "auth_login_failed"
    AUTH_LOGOUT = "auth_logout"
    AUTH_TOKEN_REFRESHED = "auth_token_refreshed"
    AUTH_PASSWORD_CHANGED = "auth_password_changed"
    # Closing an account removes the sign-in credential. The clinical record is retained under its
    # pseudonym, so this is an account event and never a clinical one — see docs/decisions.md.
    AUTH_ACCOUNT_CLOSED = "auth_account_closed"
    AUTH_REFRESH_REUSE_DETECTED = "auth_refresh_reuse_detected"
    USER_REGISTERED = "user_registered"
    CLINICIAN_ACCESS_DENIED = "clinician_access_denied"


class PregnancyAnswer(str, Enum):
    """Three-valued on purpose.

    A patient who declines to answer has given a different answer from "no". Collapsing the two
    would record a statement they did not make, and only ``YES`` closes the safety gate — see
    ``docs/decisions.md``.
    """

    YES = "yes"
    NO = "no"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class SexAtBirth(str, Enum):
    """PM spec ONB-01. Three options, matching the form."""

    FEMALE = "female"
    MALE = "male"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class HypertensionStatus(str, Enum):
    """PM spec ONB-03. Reported by the patient, never inferred."""

    DIAGNOSED = "diagnosed"
    NOT_DIAGNOSED = "not_diagnosed"
    NOT_SURE = "not_sure"


class MedicationStatusToday(str, Enum):
    """PM spec CTX-01.

    Four-valued: "not applicable" and "not sure" are different from each other and from no, and
    collapsing them would record a statement the patient did not make.
    """

    AS_USUAL = "as_usual"
    MISSED_OR_LATE = "missed_or_late"
    NOT_APPLICABLE = "not_applicable"
    NOT_SURE = "not_sure"


class CheckMode(str, Enum):
    """PM spec section 28. The two product loops."""

    SENSOR = "sensor"
    BP_ONLY = "bp_only"


class CheckSessionStatus(str, Enum):
    """PM spec section 28 and the section 31 state machine."""

    CREATED = "created"
    REFERENCE_PENDING = "reference_pending"
    PRECHECK_PENDING = "precheck_pending"
    CONTEXT_PENDING = "context_pending"
    CAPTURE_PENDING = "capture_pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    FAILED_QUALITY = "failed_quality"


class BpReferenceStatus(str, Enum):
    """PM spec section 28's ``bp_references.status``.

    Exactly one row per patient may be ``active``; the partial unique index enforces it, the same
    way :class:`CalibrationStatus` does for calibrations.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"


class BpReferenceRefreshReason(str, Enum):
    """PM spec section 28's refresh reasons, verbatim.

    Recorded rather than inferred: the reason a reference was replaced is a fact about why, and
    section 27's monitoring-gap rule reads it back.
    """

    FIRST_REFERENCE = "first_reference"
    MONITORING_GAP = "monitoring_gap"
    MEDICATION_CHANGE = "medication_change"
    PERSISTENT_TREND = "persistent_trend"
    MANUAL_REFRESH = "manual_refresh"
    HEALTH_CHANGE = "health_change"


class MedicationStatus(str, Enum):
    """PM spec section 28's ``medications.status``.

    The reason ``DELETE /medications/{id}`` does not need to delete anything: a medication someone
    stopped taking is not a row that never existed, and the history of what was being taken when a
    reading was recorded is part of reading that record later.
    """

    ACTIVE = "active"
    STOPPED = "stopped"
