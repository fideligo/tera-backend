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
    CALIBRATION_ESTABLISHED = "calibration_established"
    CALIBRATION_SUPERSEDED = "calibration_superseded"
    EVENT_RECORDED = "event_recorded"
    TIMELINE_VIEWED = "timeline_viewed"
    SUMMARY_GENERATED = "summary_generated"
    AUTH_TOKEN_ISSUED = "auth_token_issued"
    AUTH_LOGIN_FAILED = "auth_login_failed"
    AUTH_LOGOUT = "auth_logout"
    AUTH_TOKEN_REFRESHED = "auth_token_refreshed"
    AUTH_REFRESH_REUSE_DETECTED = "auth_refresh_reuse_detected"
    USER_REGISTERED = "user_registered"
    CLINICIAN_ACCESS_DENIED = "clinician_access_denied"
