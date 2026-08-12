"""SQLAlchemy models for Tera.

Importing this package registers every table on ``Base.metadata``, which Alembic's ``env.py``
relies on for autogenerate and which the tests introspect.
"""

from app.models.auth import RefreshToken
from app.models.ratelimit import RateLimitCounter
from app.models.base import Base, SyntheticMixin, utcnow
from app.models.clinical import (
    BpReference,
    ClinicianSummary,
    CuffReading,
    CheckSession,
    Medication,
    MedicationEvent,
    PatientContext,
    PhrProfile,
    Precondition,
    SessionContext,
    RedFlagEvent,
    SymptomEvent,
)
from app.models.core import AppUser, AuditLog, MonitoringEpisode, Patient
from app.models.device import Calibration, CalibrationSourceSession, DeviceProfile
from app.models.enums import (
    AuditAction,
    BpReferenceRefreshReason,
    BpReferenceStatus,
    CalibrationStatus,
    CameraHardwareLevel,
    CuffSource,
    DeviationState,
    EventType,
    CheckMode,
    CheckSessionStatus,
    HypertensionStatus,
    MedicationStatus,
    MedicationStatusToday,
    Posture,
    PregnancyAnswer,
    SexAtBirth,
    QualifiedStatus,
    RejectionReason,
    SessionStatus,
    TimestampSource,
    TrendDirection,
    UserRole,
)
from app.models.session import (
    PTT_ARRAY_DB_CEILING,
    MeasurementSession,
    SessionNonce,
    TrendEstimate,
)

#: Tables holding clinical records. Invariant 5 makes these append-only, enforced by a database
#: trigger installed in the initial migration. ``calibration`` is handled separately: its
#: supersession columns are the one sanctioned mutation.
#:
#: Must stay in step with ``APPEND_ONLY_TABLES`` in
#: ``alembic/versions/0001_initial_schema.py`` — a table listed there but not here would go
#: untested, and a table listed here but not there would have no trigger.
#: ``test_clinical_tables_match_the_migrations_trigger_list`` holds the two together.
CLINICAL_TABLES: tuple[str, ...] = (
    "measurement_session",
    "trend_estimate",
    "cuff_reading",
    "medication_event",
    "symptom_event",
    "red_flag_event",
    "clinician_summary",
    "audit_log",
    "calibration_source_session",
    "patient_context",
    "session_context",
    "precondition",
)

__all__ = [
    "Base",
    "SyntheticMixin",
    "utcnow",
    "AppUser",
    "AuditLog",
    "AuditAction",
    "Calibration",
    "CalibrationSourceSession",
    "CalibrationStatus",
    "CameraHardwareLevel",
    "ClinicianSummary",
    "CuffReading",
    "CuffSource",
    "DeviceProfile",
    "DeviationState",
    "EventType",
    "MeasurementSession",
    "BpReference",
    "BpReferenceRefreshReason",
    "BpReferenceStatus",
    "Medication",
    "MedicationStatus",
    "MedicationEvent",
    "MonitoringEpisode",
    "Patient",
    "Posture",
    "PTT_ARRAY_DB_CEILING",
    "QualifiedStatus",
    "RedFlagEvent",
    "RateLimitCounter",
    "RefreshToken",
    "RejectionReason",
    "SessionNonce",
    "SessionStatus",
    "SymptomEvent",
    "TimestampSource",
    "TrendDirection",
    "TrendEstimate",
    "UserRole",
    "CLINICAL_TABLES",
    "PatientContext",
    "CheckSession",
    "CheckMode",
    "CheckSessionStatus",
    "PhrProfile",
    "Precondition",
    "PregnancyAnswer",
    "SessionContext",
    "SexAtBirth",
    "HypertensionStatus",
    "MedicationStatusToday",
]
