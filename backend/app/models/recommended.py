import uuid
from datetime import date, datetime
from sqlalchemy import String, Boolean, Float, Date, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UuidPkMixin, utcnow

class User(UuidPkMixin, Base):
    __tablename__ = "users"
    
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    auth_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    phr_profile: Mapped["PhrProfileB2C"] = relationship(back_populates="user", uselist=False)
    health_conditions: Mapped[list["HealthCondition"]] = relationship(back_populates="user")
    medications: Mapped[list["MedicationModel"]] = relationship(back_populates="user")
    lifestyle_profile: Mapped["LifestyleProfile"] = relationship(back_populates="user", uselist=False)
    family_history: Mapped[list["FamilyHistory"]] = relationship(back_populates="user")


class PhrProfileB2C(UuidPkMixin, Base):
    # `PhrProfile` is already a mapped class in `clinical.py`. A duplicate *class* name is a
    # second, quieter version of the duplicate table name above: SQLAlchemy's declarative registry
    # cannot resolve the string `"PhrProfile"` in a relationship when two classes answer to it, so
    # it raised on the first request that touched a mapper rather than at import — which is why
    # the API booted and then 500ed on register-patient.
    __tablename__ = "phr_profiles"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    sex_assigned_at_birth: Mapped[str | None] = mapped_column(String, nullable=True)
    height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    
    pregnancy_status: Mapped[str | None] = mapped_column(String, nullable=True)
    postpartum_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    arrhythmia_status: Mapped[str | None] = mapped_column(String, nullable=True)
    hypertension_status: Mapped[str | None] = mapped_column(String, nullable=True)
    taking_bp_medication: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    
    user: Mapped["User"] = relationship(back_populates="phr_profile")


class HealthCondition(UuidPkMixin, Base):
    __tablename__ = "health_conditions"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    
    condition_code: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    diagnosed_at_optional: Mapped[date | None] = mapped_column(Date, nullable=True)
    
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    
    user: Mapped["User"] = relationship(back_populates="health_conditions")


class MedicationModel(UuidPkMixin, Base):
    __tablename__ = "medications"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    
    name: Mapped[str] = mapped_column(String, nullable=False)
    dose: Mapped[str | None] = mapped_column(String, nullable=True)
    dose_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    frequency: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_changed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    
    user: Mapped["User"] = relationship(back_populates="medications")


class LifestyleProfile(UuidPkMixin, Base):
    __tablename__ = "lifestyle_profiles"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    
    physical_activity_level: Mapped[str | None] = mapped_column(String, nullable=True)
    smoking_status: Mapped[str | None] = mapped_column(String, nullable=True)
    nicotine_type: Mapped[str | None] = mapped_column(String, nullable=True)
    alcohol_frequency: Mapped[str | None] = mapped_column(String, nullable=True)
    usual_sleep_hours: Mapped[str | None] = mapped_column(String, nullable=True)
    usual_stress_level: Mapped[str | None] = mapped_column(String, nullable=True)
    
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    
    user: Mapped["User"] = relationship(back_populates="lifestyle_profile")


class FamilyHistory(UuidPkMixin, Base):
    __tablename__ = "family_history"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    
    condition: Mapped[str] = mapped_column(String, nullable=False)
    relationship_type: Mapped[str] = mapped_column(String, name="relationship", nullable=False)
    early_onset_boolean_optional: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    
    user: Mapped["User"] = relationship(back_populates="family_history")

class Device(UuidPkMixin, Base):
    __tablename__ = "devices"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    os_version: Mapped[str] = mapped_column(String, nullable=False)
    app_version: Mapped[str] = mapped_column(String, nullable=False)
    eligibility_status: Mapped[str] = mapped_column(String, nullable=False)
    camera_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    flash_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    accelerometer_supported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    user: Mapped["User"] = relationship()


class CheckSessionB2C(UuidPkMixin, Base):
    __tablename__ = "check_sessions"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    bp_reference_id_optional: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    
    user: Mapped["User"] = relationship()
    device: Mapped["Device"] = relationship()


class PreconditionB2C(UuidPkMixin, Base):
    __tablename__ = "preconditions"
    
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("check_sessions.id"), nullable=False)
    rested_5_min: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recent_activity_30_min: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recent_caffeine_30_min: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recent_nicotine_30_min: Mapped[bool] = mapped_column(Boolean, nullable=False)
    needs_restroom: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    
    session: Mapped["CheckSessionB2C"] = relationship()


class SessionContextB2C(UuidPkMixin, Base):
    # `session_context` is already taken by the append-only table in `clinical.py` (migration
    # 0009). Two classes claiming one table name is not a duplicate definition SQLAlchemy can
    # resolve — it raises on import, which took the whole API down: `docker compose up` could not
    # boot and `pytest` could not collect a single test.
    #
    # Renamed rather than merged, because the two are genuinely different records: the clinical
    # one is append-only with a `synthetic` flag and an episode behind it, this one is the B2C
    # shape keyed on `check_sessions`. Merging them is a schema decision, not an import fix.
    __tablename__ = "session_context_b2c"

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("check_sessions.id"), nullable=False)
    sleep_less_than_usual: Mapped[bool] = mapped_column(Boolean, nullable=False)
    stress_higher_than_usual: Mapped[bool] = mapped_column(Boolean, nullable=False)
    feeling_unwell: Mapped[bool] = mapped_column(Boolean, nullable=False)
    symptoms_json: Mapped[str | None] = mapped_column(String, nullable=True)
    medication_status_today: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    
    session: Mapped["CheckSessionB2C"] = relationship()

class SensorMeasurementB2C(UuidPkMixin, Base):
    __tablename__ = "sensor_measurements"
    
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("check_sessions.id"), nullable=False)
    raw_scg_storage_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_ppg_storage_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    ptt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    heart_rate_bpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capture_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    algorithm_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    
    session: Mapped["CheckSessionB2C"] = relationship()


class SignalQualityB2C(UuidPkMixin, Base):
    __tablename__ = "signal_quality"
    
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("check_sessions.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    quality_metrics_json: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    
    session: Mapped["CheckSessionB2C"] = relationship()


class TrendResultB2C(UuidPkMixin, Base):
    __tablename__ = "trend_results"
    
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("check_sessions.id"), nullable=False)
    trend_state: Mapped[str] = mapped_column(String, nullable=False)
    comparison_window: Mapped[str | None] = mapped_column(String, nullable=True)
    baseline_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    technical_details_json: Mapped[str | None] = mapped_column(String, nullable=True)
    model_or_rule_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    
    session: Mapped["CheckSessionB2C"] = relationship()


class InsightB2C(UuidPkMixin, Base):
    __tablename__ = "insights"
    
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("check_sessions.id"), nullable=False)
    result_state: Mapped[str] = mapped_column(String, nullable=False)
    interpretation_code: Mapped[str | None] = mapped_column(String, nullable=True)
    priority_action_code: Mapped[str | None] = mapped_column(String, nullable=True)
    recommendation_codes_json: Mapped[str | None] = mapped_column(String, nullable=True)
    monitoring_plan_code: Mapped[str | None] = mapped_column(String, nullable=True)
    followup_code: Mapped[str | None] = mapped_column(String, nullable=True)
    
    session: Mapped["CheckSessionB2C"] = relationship()


class BpReadingB2C(UuidPkMixin, Base):
    __tablename__ = "bp_readings"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    session_id_optional: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("check_sessions.id"), nullable=True)
    systolic: Mapped[int] = mapped_column(Integer, nullable=False)
    diastolic: Mapped[int] = mapped_column(Integer, nullable=False)
    pulse: Mapped[int | None] = mapped_column(Integer, nullable=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_manual_or_ocr: Mapped[str] = mapped_column(String, nullable=False)
    ocr_confidence_optional: Mapped[float | None] = mapped_column(Float, nullable=True)
    user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    used_as_reference: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    
    user: Mapped["User"] = relationship()
    session: Mapped["CheckSessionB2C"] = relationship()


class BpReferenceB2C(UuidPkMixin, Base):
    __tablename__ = "bp_references"
    
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    bp_reading_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bp_readings.id"), nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    deactivated_at_optional: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    
    user: Mapped["User"] = relationship()
    bp_reading: Mapped["BpReadingB2C"] = relationship()
