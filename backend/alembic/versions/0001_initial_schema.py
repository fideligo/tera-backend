"""Initial Tera schema.

Creates every table in BUILD_SPEC 4.1 together with the constraints that section requires to
exist "at the database level, not only in application code", plus the append-only triggers that
make invariants 4 and 5 true rather than merely intended.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-08-07

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Tables holding clinical records. Invariant 5: "Clinical records are append-only. No update or
#: delete endpoint on clinical rows. Corrections are new rows referencing the original. The audit
#: log is append-only."
#:
#: The absence of a route is not enforcement — a migration, a console session or a future
#: developer's convenience helper can all issue an UPDATE. The trigger makes the property hold
#: regardless of who is connected.
APPEND_ONLY_TABLES: tuple[str, ...] = (
    "measurement_session",
    "trend_estimate",
    "cuff_reading",
    "medication_event",
    "symptom_event",
    "red_flag_event",
    "clinician_summary",
    "audit_log",
    "calibration_source_session",
)

#: Postgres enum type names, dropped in reverse on downgrade.
ENUM_TYPES: tuple[str, ...] = (
    "user_role",
    "audit_action",
    "camera_hardware_level",
    "timestamp_source",
    "qualified_status",
    "calibration_status",
    "posture",
    "session_status",
    "rejection_reason",
    "trend_direction",
    "deviation_state",
    "cuff_source",
)

APPEND_ONLY_GUARD_SQL = """
CREATE OR REPLACE FUNCTION tera_append_only_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'table % holds clinical records and is append-only (invariant 5): % is not permitted. '
        'Corrections are new rows referencing the original.',
        TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;
"""

#: Calibration is the one sanctioned exception to blanket append-only. Invariant 4 requires
#: supersession bookkeeping to be written to the old row ("status active/superseded,
#: superseded_by self-FK") while also requiring that recalibration "never mutates history".
#: This trigger draws the line exactly: the baseline is immutable, the supersession pointer is
#: not, and supersession is one-way.
CALIBRATION_GUARD_SQL = """
CREATE OR REPLACE FUNCTION tera_calibration_history_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'calibration rows are append-only (invariant 4): DELETE is not permitted'
            USING ERRCODE = 'raise_exception';
    END IF;

    IF NEW.id                       IS DISTINCT FROM OLD.id
    OR NEW.patient_id               IS DISTINCT FROM OLD.patient_id
    OR NEW.device_profile_id        IS DISTINCT FROM OLD.device_profile_id
    OR NEW.reference_cuff_reading_id IS DISTINCT FROM OLD.reference_cuff_reading_id
    OR NEW.baseline_mean_ms         IS DISTINCT FROM OLD.baseline_mean_ms
    OR NEW.baseline_sd_ms           IS DISTINCT FROM OLD.baseline_sd_ms
    OR NEW.n_sessions               IS DISTINCT FROM OLD.n_sessions
    OR NEW.established_at           IS DISTINCT FROM OLD.established_at
    OR NEW.synthetic                IS DISTINCT FROM OLD.synthetic THEN
        RAISE EXCEPTION
            'calibration history is immutable (invariant 4): only status, superseded_by_id and '
            'superseded_at may change. Recalibration inserts a new row.'
            USING ERRCODE = 'raise_exception';
    END IF;

    -- Supersession is final. Reactivating a superseded calibration would let an estimate be
    -- reinterpreted against a baseline that had already been retired.
    IF OLD.status = 'superseded' THEN
        RAISE EXCEPTION
            'calibration % is already superseded (invariant 4): supersession is one-way', OLD.id
            USING ERRCODE = 'raise_exception';
    END IF;

    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    # ---------------------------------------------------------------- root tables
    op.create_table(
        "audit_log",
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("role", sa.Enum("patient", "clinician", "admin", name="user_role"), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "device_profile_submitted", "nonce_issued", "session_submitted",
                "session_duplicate_replayed", "cuff_reading_recorded", "calibration_established",
                "calibration_superseded", "event_recorded", "timeline_viewed",
                "summary_generated", "auth_token_issued", name="audit_action",
            ),
            nullable=False,
        ),
        sa.Column("target", sa.String(length=128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_log_action"), "audit_log", ["action"], unique=False)
    op.create_index(op.f("ix_audit_log_actor"), "audit_log", ["actor"], unique=False)
    op.create_index(op.f("ix_audit_log_occurred_at"), "audit_log", ["occurred_at"], unique=False)

    op.create_table(
        "patient",
        sa.Column("pseudonym", sa.String(length=64), nullable=False),
        sa.Column("clinic_id", sa.String(length=64), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pseudonym"),
    )
    op.create_index(op.f("ix_patient_clinic_id"), "patient", ["clinic_id"], unique=False)
    op.create_index(op.f("ix_patient_synthetic"), "patient", ["synthetic"], unique=False)

    op.create_table(
        "session_nonce",
        sa.Column("value", sa.String(length=64), nullable=False),
        sa.Column("issued_to", sa.String(length=128), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_session_nonce_issued_to"), "session_nonce", ["issued_to"], unique=False)
    op.create_index(op.f("ix_session_nonce_value"), "session_nonce", ["value"], unique=True)

    op.create_table(
        "app_user",
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.Enum("patient", "clinician", "admin", name="user_role"), nullable=False),
        sa.Column("clinic_id", sa.String(length=64), nullable=True),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "(role = 'patient') = (patient_id IS NOT NULL)",
            name="ck_app_user_patient_link_matches_role",
        ),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject"),
    )
    op.create_index(op.f("ix_app_user_clinic_id"), "app_user", ["clinic_id"], unique=False)
    op.create_index(op.f("ix_app_user_patient_id"), "app_user", ["patient_id"], unique=False)
    op.create_index(op.f("ix_app_user_synthetic"), "app_user", ["synthetic"], unique=False)

    op.create_table(
        "device_profile",
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("os_version", sa.String(length=64), nullable=False),
        sa.Column("accel_rate_hz", sa.Float(), nullable=False),
        sa.Column("camera_fps", sa.Float(), nullable=False),
        sa.Column(
            "camera_hw_level",
            sa.Enum("legacy", "limited", "full", "level_3", "external", name="camera_hardware_level"),
            nullable=False,
        ),
        sa.Column("manual_sensor", sa.Boolean(), nullable=False),
        sa.Column("timestamp_source", sa.Enum("unknown", "realtime", name="timestamp_source"), nullable=False),
        sa.Column("clock_offset_sd_ms", sa.Float(), nullable=False),
        sa.Column(
            "qualified_status",
            sa.Enum("qualified", "provisional", "not_qualified", name="qualified_status"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("accel_rate_hz > 0", name="ck_device_profile_accel_rate_positive"),
        sa.CheckConstraint("camera_fps > 0", name="ck_device_profile_camera_fps_positive"),
        sa.CheckConstraint("clock_offset_sd_ms >= 0", name="ck_device_profile_clock_offset_sd_non_negative"),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_device_profile_patient_id"), "device_profile", ["patient_id"], unique=False)
    op.create_index(op.f("ix_device_profile_synthetic"), "device_profile", ["synthetic"], unique=False)

    op.create_table(
        "monitoring_episode",
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("reviewing_clinician_id", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "protocol_params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint("ended_at IS NULL OR ended_at > started_at", name="ck_episode_end_after_start"),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewing_clinician_id"], ["app_user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_monitoring_episode_patient_id"), "monitoring_episode", ["patient_id"], unique=False)
    op.create_index(
        op.f("ix_monitoring_episode_reviewing_clinician_id"),
        "monitoring_episode", ["reviewing_clinician_id"], unique=False,
    )
    op.create_index(op.f("ix_monitoring_episode_synthetic"), "monitoring_episode", ["synthetic"], unique=False)

    # ---------------------------------------------------------------- clinical tables
    op.create_table(
        "clinician_summary",
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contents", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(["episode_id"], ["monitoring_episode.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_clinician_summary_episode_id"), "clinician_summary", ["episode_id"], unique=False)
    op.create_index(op.f("ix_clinician_summary_synthetic"), "clinician_summary", ["synthetic"], unique=False)

    # The only table in the system that holds mmHg (invariant 1). The plausibility ranges are
    # required at database level by BUILD_SPEC 4.1.
    op.create_table(
        "cuff_reading",
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("systolic_mmhg", sa.Integer(), nullable=False),
        sa.Column("diastolic_mmhg", sa.Integer(), nullable=False),
        sa.Column("pulse_bpm", sa.Integer(), nullable=True),
        sa.Column("source", sa.Enum("manual_entry", "photograph", name="cuff_source"), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corrects_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(source = 'photograph') OR (ocr_confidence IS NULL)",
            name="ck_cuff_ocr_confidence_only_for_photograph",
        ),
        sa.CheckConstraint("corrects_id IS NULL OR corrects_id <> id", name="ck_cuff_not_self_correcting"),
        sa.CheckConstraint("diastolic_mmhg BETWEEN 30 AND 200", name="ck_cuff_diastolic_plausible"),
        sa.CheckConstraint("pulse_bpm IS NULL OR pulse_bpm BETWEEN 25 AND 250", name="ck_cuff_pulse_plausible"),
        sa.CheckConstraint("systolic_mmhg > diastolic_mmhg", name="ck_cuff_systolic_above_diastolic"),
        sa.CheckConstraint("systolic_mmhg BETWEEN 50 AND 300", name="ck_cuff_systolic_plausible"),
        sa.ForeignKeyConstraint(["corrects_id"], ["cuff_reading.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["episode_id"], ["monitoring_episode.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cuff_reading_episode_id"), "cuff_reading", ["episode_id"], unique=False)
    op.create_index(op.f("ix_cuff_reading_synthetic"), "cuff_reading", ["synthetic"], unique=False)
    op.create_index(op.f("ix_cuff_reading_taken_at"), "cuff_reading", ["taken_at"], unique=False)

    for event_table in ("medication_event", "symptom_event", "red_flag_event"):
        op.create_table(
            event_table,
            sa.Column("episode_id", sa.Uuid(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["episode_id"], ["monitoring_episode.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f(f"ix_{event_table}_episode_id"), event_table, ["episode_id"], unique=False)
        op.create_index(op.f(f"ix_{event_table}_occurred_at"), event_table, ["occurred_at"], unique=False)
        op.create_index(op.f(f"ix_{event_table}_synthetic"), event_table, ["synthetic"], unique=False)

    op.create_table(
        "calibration",
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("device_profile_id", sa.Uuid(), nullable=False),
        sa.Column("reference_cuff_reading_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_mean_ms", sa.Float(), nullable=False),
        sa.Column("baseline_sd_ms", sa.Float(), nullable=False),
        sa.Column("n_sessions", sa.Integer(), nullable=False),
        sa.Column("status", sa.Enum("active", "superseded", name="calibration_status"), nullable=False),
        sa.Column("superseded_by_id", sa.Uuid(), nullable=True),
        sa.Column("established_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "(status = 'superseded') = (superseded_at IS NOT NULL)",
            name="ck_calibration_superseded_at_matches_status",
        ),
        sa.CheckConstraint(
            "(status = 'superseded') = (superseded_by_id IS NOT NULL)",
            name="ck_calibration_superseded_by_matches_status",
        ),
        # BUILD_SPEC 4.1 requires both of these at database level.
        sa.CheckConstraint("baseline_sd_ms > 0", name="ck_calibration_baseline_sd_positive"),
        sa.CheckConstraint("n_sessions >= 3", name="ck_calibration_min_sessions"),
        sa.CheckConstraint(
            "superseded_by_id IS NULL OR superseded_by_id <> id",
            name="ck_calibration_not_self_superseding",
        ),
        sa.ForeignKeyConstraint(["device_profile_id"], ["device_profile.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reference_cuff_reading_id"], ["cuff_reading.id"], ondelete="RESTRICT"),
        # Deferred: supersession marks the old row superseded before the new row exists, so the
        # partial unique index below never sees two active calibrations. See
        # app/models/device.py for the full reasoning.
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["calibration.id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_calibration_superseded_by_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_calibration_device_profile_id"), "calibration", ["device_profile_id"], unique=False)
    op.create_index(op.f("ix_calibration_established_at"), "calibration", ["established_at"], unique=False)
    op.create_index(op.f("ix_calibration_patient_id"), "calibration", ["patient_id"], unique=False)
    op.create_index(op.f("ix_calibration_synthetic"), "calibration", ["synthetic"], unique=False)
    # Invariant 4 — at most one active calibration per patient per device. BUILD_SPEC 4.1 names
    # this as a partial unique index specifically.
    op.create_index(
        "uq_calibration_one_active_per_patient_device",
        "calibration",
        ["patient_id", "device_profile_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "measurement_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("device_profile_id", sa.Uuid(), nullable=False),
        sa.Column("calibration_id", sa.Uuid(), nullable=True),
        sa.Column("model_version", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "posture",
            sa.Enum("seated", "supine", "semi_recumbent", "standing", name="posture"),
            nullable=False,
        ),
        sa.Column("status", sa.Enum("completed", "rejected", name="session_status"), nullable=False),
        sa.Column(
            "rejection_reason",
            sa.Enum(
                "poor_signal_quality", "insufficient_beats", "excessive_motion", "posture_unstable",
                "torch_unavailable", "sensor_rate_below_qualified", "clock_unstable", "user_aborted",
                "red_flag_reported", "implausible_payload", "no_active_calibration",
                name="rejection_reason",
            ),
            nullable=True,
        ),
        sa.Column("n_beats_total", sa.Integer(), nullable=False),
        sa.Column("n_beats_usable", sa.Integer(), nullable=False),
        sa.Column("ptt_ms", postgresql.ARRAY(sa.REAL()), server_default=sa.text("'{}'::real[]"), nullable=False),
        sa.Column("quality", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # Invariant 3, verbatim from BUILD_SPEC 4.1.
        sa.CheckConstraint(
            "(status = 'rejected') = (rejection_reason IS NOT NULL)",
            name="ck_session_rejection_reason_matches_status",
        ),
        sa.CheckConstraint("n_beats_total >= 0", name="ck_session_beats_non_negative"),
        sa.CheckConstraint("n_beats_usable <= n_beats_total", name="ck_session_usable_not_above_total"),
        sa.CheckConstraint("n_beats_usable >= 0", name="ck_session_usable_non_negative"),
        # Invariant 2 — this column must not become a channel for waveform data. See
        # app/models/session.py::PTT_ARRAY_DB_CEILING for why the ceiling lives in a migration.
        sa.CheckConstraint("ptt_ms IS NULL OR array_ndims(ptt_ms) <= 1", name="ck_session_ptt_1d"),
        sa.CheckConstraint(
            "ptt_ms IS NULL OR coalesce(array_length(ptt_ms, 1), 0) <= 300",
            name="ck_session_ptt_array_length_bounded",
        ),
        sa.ForeignKeyConstraint(["calibration_id"], ["calibration.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["device_profile_id"], ["device_profile.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["episode_id"], ["monitoring_episode.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_measurement_session_calibration_id"), "measurement_session", ["calibration_id"], unique=False
    )
    op.create_index(
        op.f("ix_measurement_session_device_profile_id"), "measurement_session", ["device_profile_id"], unique=False
    )
    op.create_index(op.f("ix_measurement_session_episode_id"), "measurement_session", ["episode_id"], unique=False)
    op.create_index(op.f("ix_measurement_session_started_at"), "measurement_session", ["started_at"], unique=False)
    op.create_index(op.f("ix_measurement_session_status"), "measurement_session", ["status"], unique=False)
    op.create_index(op.f("ix_measurement_session_synthetic"), "measurement_session", ["synthetic"], unique=False)

    op.create_table(
        "calibration_source_session",
        sa.Column("calibration_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("session_ptt_ms", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["calibration_id"], ["calibration.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["measurement_session.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("calibration_id", "session_id"),
    )

    # Invariant 1 — there is no systolic or diastolic column here, and
    # test_trend_estimate_has_no_pressure_column introspects the live schema to keep it so.
    op.create_table(
        "trend_estimate",
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("calibration_id", sa.Uuid(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("stable", "increase", "decrease", name="trend_direction"),
            nullable=False,
        ),
        sa.Column("magnitude_sd", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "deviation_state",
            sa.Enum("none", "possible", "persistent", name="deviation_state"),
            nullable=False,
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "(direction = 'stable') = (deviation_state = 'none')",
            name="ck_estimate_direction_matches_deviation_state",
        ),
        sa.CheckConstraint("confidence > 0 AND confidence < 1", name="ck_estimate_confidence_open_unit_interval"),
        sa.CheckConstraint("magnitude_sd >= 0", name="ck_estimate_magnitude_non_negative"),
        sa.ForeignKeyConstraint(["calibration_id"], ["calibration.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["measurement_session.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_trend_estimate_calibration_id"), "trend_estimate", ["calibration_id"], unique=False)
    op.create_index(op.f("ix_trend_estimate_session_id"), "trend_estimate", ["session_id"], unique=True)
    op.create_index(op.f("ix_trend_estimate_synthetic"), "trend_estimate", ["synthetic"], unique=False)

    # ---------------------------------------------------------------- append-only enforcement
    op.execute(APPEND_ONLY_GUARD_SQL)
    op.execute(CALIBRATION_GUARD_SQL)

    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION tera_append_only_guard();
            """
        )

    op.execute(
        """
        CREATE TRIGGER trg_calibration_history_guard
        BEFORE UPDATE OR DELETE ON calibration
        FOR EACH ROW EXECUTE FUNCTION tera_calibration_history_guard();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_calibration_history_guard ON calibration;")
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    op.execute("DROP FUNCTION IF EXISTS tera_calibration_history_guard();")
    op.execute("DROP FUNCTION IF EXISTS tera_append_only_guard();")

    op.drop_table("trend_estimate")
    op.drop_table("calibration_source_session")
    op.drop_table("measurement_session")
    op.drop_table("calibration")
    for event_table in ("red_flag_event", "symptom_event", "medication_event"):
        op.drop_table(event_table)
    op.drop_table("cuff_reading")
    op.drop_table("clinician_summary")
    op.drop_table("monitoring_episode")
    op.drop_table("device_profile")
    op.drop_table("app_user")
    op.drop_table("session_nonce")
    op.drop_table("patient")
    op.drop_table("audit_log")

    for enum_name in reversed(ENUM_TYPES):
        op.execute(f"DROP TYPE IF EXISTS {enum_name};")
