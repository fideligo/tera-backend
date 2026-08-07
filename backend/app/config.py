"""Configuration for Tera.

Invariant 10: *all clinical thresholds are configuration with documented defaults, never
hard-coded magic numbers, and every default carries a source comment explaining where it came
from.* Nothing in `app/services/` or `app/api/` may inline a clinical number — it comes from
here, or from the per-episode override in ``monitoring_episode.protocol_params``.

On the provenance of the defaults, stated plainly because invariant 9 forbids passing off
invented numbers as established ones: the *physiological* bounds below (PTT range, plausibility
ranges for cuff readings) come from the published ranges cited in each comment. The *engineering*
thresholds (device qualification bands, array length bound, rate limits, confidence weights) are
design choices made for this build, reasoned from the measurement requirement and labelled as
such. None of them has been validated against clinical outcome data, and none should be presented
as if it had.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Hard upper bound on ``DeviationSettings.confidence_ceiling``. Not a default — a limit.
#: See the comment on that field for why this one threshold is not tunable upward.
CONFIDENCE_CEILING_LIMIT = 0.95


class DeviationSettings(BaseSettings):
    """Thresholds for the deviation engine (BUILD_SPEC 4.3)."""

    model_config = SettingsConfigDict(env_prefix="TERA_DEVIATION_")

    # BUILD_SPEC 4.3 states the default explicitly: "default k = 2, configurable per episode".
    # Two standard deviations of a patient's own baseline is the conventional
    # "outside usual variation" line; it is a statistical convention, not a clinical cut-off.
    # Bounded above zero: k = 0 would make every session a deviation, which is noisy but safe,
    # while a negative k is meaningless and would invert the comparison silently.
    deviation_k: float = Field(default=2.0, gt=0.0)

    # BUILD_SPEC 4.3: "a repeat session within the configured window". 48 h is chosen so a
    # patient who measures once or twice daily has a realistic chance of producing the repeat
    # without the pair spanning so much time that they describe different physiological states.
    # The spec requires the window but does not name a value; this is a design choice.
    persistence_window_hours: int = 48

    # Trimming rule is fixed by BUILD_SPEC 4.3 ("discard beyond 1.5 x IQR"), the standard Tukey
    # fence. Exposed as configuration so it is not a literal in the engine.
    iqr_fence_multiplier: float = 1.5

    # A baseline needs at least three accepted calibration sessions (BUILD_SPEC 4.1, enforced as
    # a DB CHECK). Three is the spec's floor: enough to compute a standard deviation with any
    # meaning at all, and it is a floor rather than a recommendation.
    min_calibration_sessions: int = 3

    # Minimum usable beats for a session to yield an estimate. A 60 s capture at 60 bpm gives
    # ~60 beats; requiring 30 means at least half the capture survived the quality gate.
    # Engineering choice for this build. Per-episode override: protocol_params.min_beat_count.
    min_usable_beats: int = 30

    # Confidence is a documented heuristic, never a claim of clinical accuracy (BUILD_SPEC 4.3).
    # It is capped strictly below 1.0 so no response can read as certainty.
    #
    # The ceiling is bounded at CONFIDENCE_CEILING_LIMIT and cannot be raised past it, by
    # anyone, at any deployment. Every other threshold in this file is a clinical judgement a
    # clinic may legitimately want to tune; this one is not. Raising it toward 1.0 would not
    # change what the number *is* — a blunt ordering of sessions by how much usable signal they
    # produced — but it would change what it *looks like*, and a reader who sees 0.99 will read
    # certainty into a heuristic that cannot support it. That is invariant 6 by the back door:
    # not a diagnosis, but a claim of accuracy the method does not have.
    #
    # Lowering it is always allowed. There is no floor on modesty.
    confidence_ceiling: float = Field(default=0.95, gt=0.0, le=CONFIDENCE_CEILING_LIMIT)
    confidence_floor: float = Field(default=0.10, gt=0.0, lt=1.0)
    # Relative weight of beat quantity vs. signal quality in the heuristic. Equal weighting is
    # the honest default: we have no evidence that one dominates the other. They must sum to 1,
    # or the weighted score saturates early or never reaches the ceiling, and the resulting
    # numbers would not mean what the docstring says they mean.
    confidence_beat_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence_quality_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    # Beat count stops adding confidence at twice the minimum. Beyond that the limiting factor
    # is signal quality, not sample size. Engineering choice for this build.
    confidence_beat_saturation_multiple: float = 2.0
    # SNR range over which the quality term moves from 0 to 1. Below the floor the pulse is not
    # separable from noise; above the ceiling more SNR does not make the fiducial point easier
    # to locate. Engineering choices, in the dB units the device gate reports.
    confidence_snr_db_floor: float = 0.0
    confidence_snr_db_ceiling: float = 20.0

    @model_validator(mode="after")
    def _confidence_scale_is_coherent(self) -> "DeviationSettings":
        """Reject a configuration that would make the confidence number mean something else.

        Field-level bounds stop each value going out of range on its own; these are the
        relationships between them. A deployment that got any of these wrong would still
        produce numbers, and they would still look like confidences — which is precisely why
        this fails at startup rather than degrading quietly.
        """
        if self.confidence_floor >= self.confidence_ceiling:
            raise ValueError(
                f"confidence_floor ({self.confidence_floor}) must be below confidence_ceiling "
                f"({self.confidence_ceiling}); otherwise the scale is inverted or empty"
            )

        weight_total = self.confidence_beat_weight + self.confidence_quality_weight
        if abs(weight_total - 1.0) > 1e-9:
            raise ValueError(
                f"confidence_beat_weight + confidence_quality_weight must be 1.0, got "
                f"{weight_total}. Anything else rescales the heuristic so it no longer spans "
                f"floor to ceiling, and the resulting numbers would not mean what the "
                f"documentation says they mean."
            )

        if self.confidence_snr_db_ceiling <= self.confidence_snr_db_floor:
            raise ValueError(
                "confidence_snr_db_ceiling must be above confidence_snr_db_floor"
            )

        return self


class PlausibilitySettings(BaseSettings):
    """Server-side payload plausibility (BUILD_SPEC 4.4, defence in depth).

    The quality gate runs on the device. These bounds exist because the backend must not trust
    the client.
    """

    model_config = SettingsConfigDict(env_prefix="TERA_PLAUSIBILITY_")

    # BUILD_SPEC 4.4 states the range explicitly: "PTT values outside 80-400 ms". Consistent with
    # reported pulse transit / pulse arrival times over the proximal arterial path in adults.
    ptt_min_ms: float = 80.0
    ptt_max_ms: float = 400.0

    # Invariant 2: the deepest granularity accepted is one derived interval per beat. Bounding
    # the array stops the column being used to smuggle a waveform. 300 intervals is ~5 minutes at
    # 60 bpm — far above any legitimate 60-90 s capture, far below a sample buffer.
    # Engineering choice for this build.
    max_ptt_array_length: int = 300

    # Cuff plausibility ranges are given verbatim in BUILD_SPEC 4.1 and must exist as DB CHECKs.
    # They are implausibility filters for data entry, not clinical thresholds, and are
    # deliberately wider than any range that would prompt clinical action.
    systolic_min_mmhg: int = 50
    systolic_max_mmhg: int = 300
    diastolic_min_mmhg: int = 30
    diastolic_max_mmhg: int = 200
    pulse_min_bpm: int = 25
    pulse_max_bpm: int = 250

    # A session's achieved rates must not fall below the band its device profile qualified in
    # (BUILD_SPEC 4.4). Tolerance absorbs ordinary jitter between the profiling run and a
    # capture; beyond it the capture is not comparable to the one the profile qualified.
    achieved_rate_tolerance_fraction: float = 0.10

    # Quality fields every session payload must carry; a payload missing any of them is 422
    # ("missing quality fields", BUILD_SPEC 4.4) rather than silently accepted.
    required_quality_fields: tuple[str, ...] = (
        "accel_rate_hz",
        "camera_fps",
        "dropped_frame_pct",
        "snr_db",
        "motion_index",
    )

    # Bounds on the quality metrics themselves, so a client cannot report a nonsense value to
    # pass the gate. Engineering choices matched to the units each metric is reported in.
    snr_db_min: float = -20.0
    snr_db_max: float = 60.0
    motion_index_min: float = 0.0
    motion_index_max: float = 1.0
    dropped_frame_pct_min: float = 0.0
    dropped_frame_pct_max: float = 100.0


class DeviceEligibilitySettings(BaseSettings):
    """Device qualification bands for POST /v1/device-profiles.

    These decide whether a handset can run Tera at all. They are engineering thresholds derived
    from the measurement requirement, not validated hardware benchmarks — Phase 3's profiler
    produces the measured numbers, and invariant 9 forbids inventing them here.
    """

    model_config = SettingsConfigDict(env_prefix="TERA_DEVICE_")

    # The PTT differences Tera must resolve are on the order of a few milliseconds to a few tens
    # of milliseconds. 200 Hz accelerometer sampling gives 5 ms resolution on the SCG fiducial
    # point; 100 Hz gives 10 ms, which is usable but coarse relative to the effect size, hence
    # the provisional band. Android caps sensors at 200 Hz without HIGH_SAMPLING_RATE_SENSORS
    # (Android 12+), which is why the qualified band sits at that cap.
    accel_rate_qualified_hz: float = 200.0
    accel_rate_provisional_hz: float = 100.0

    # Camera frame interval bounds PPG timing resolution: 30 fps is 33 ms per frame, so pulse
    # arrival is interpolated between frames rather than read off directly. 60 fps halves that
    # error, hence the qualified band. Below 30 fps the interpolation is doing more work than
    # the measurement.
    camera_fps_qualified: float = 60.0
    camera_fps_provisional: float = 30.0

    # Clock offset stability, not absolute offset, is what matters: a constant offset between the
    # camera and sensor time bases is absorbed by personal calibration, a drifting one is not.
    # The tolerance is set well below the smallest PTT change worth reporting.
    clock_offset_sd_qualified_ms: float = 2.0
    clock_offset_sd_provisional_ms: float = 5.0

    # Android CameraCharacteristics values. A LEGACY device cannot lock exposure reliably, and
    # unlocked auto-exposure corrupts the PPG amplitude series it is meant to measure.
    acceptable_hardware_levels_qualified: tuple[str, ...] = ("full", "level_3")
    acceptable_hardware_levels_provisional: tuple[str, ...] = ("limited",)

    # SENSOR_INFO_TIMESTAMP_SOURCE == REALTIME means camera and accelerometer timestamps share a
    # time base. Without it the two signals must be aligned through an inferred offset, which is
    # exactly the error PTT is most sensitive to.
    require_realtime_timestamp_source_for_qualified: bool = True


class SecuritySettings(BaseSettings):
    """Auth, nonce, idempotency and rate limiting (BUILD_SPEC 4.5)."""

    model_config = SettingsConfigDict(env_prefix="TERA_")

    jwt_secret: str = Field(
        default="dev-only-insecure-secret-change-me",
        description="Overridden by TERA_JWT_SECRET. Startup refuses to run with the default "
        "outside development.",
    )
    jwt_algorithm: str = "HS256"
    # Short-lived access tokens (BUILD_SPEC 4.5) — 15 minutes bounds the damage from a leaked
    # token on a shared or lost handset without forcing constant re-auth.
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 14

    # Single-use nonce TTL. Long enough to cover a capture that started before the nonce was
    # fetched and a slow upload; short enough that a captured nonce is not useful later.
    nonce_ttl_seconds: int = 900

    # Per-token and per-patient rate limits on ingest and summary (BUILD_SPEC 4.5). A patient
    # measuring on protocol submits a handful of sessions a day; these ceilings are far above
    # legitimate use and exist to bound abuse, not to shape behaviour.
    ingest_rate_limit_per_token_per_hour: int = 60
    ingest_rate_limit_per_patient_per_hour: int = 60
    summary_rate_limit_per_token_per_hour: int = 120
    nonce_rate_limit_per_token_per_hour: int = 120


class Settings(BaseSettings):
    """Top-level application settings."""

    model_config = SettingsConfigDict(
        env_prefix="TERA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # Port 5434 is what docker-compose.yml publishes the db service on.
    database_url: str = "postgresql+psycopg://tera:tera_dev_password@localhost:5434/tera"
    test_database_url: str = (
        "postgresql+psycopg://tera:tera_dev_password@localhost:5434/tera_test"
    )

    demo_patient_password: str = "demo-patient-password"
    demo_clinician_password: str = "demo-clinician-password"

    deviation: DeviationSettings = Field(default_factory=DeviationSettings)
    plausibility: PlausibilitySettings = Field(default_factory=PlausibilitySettings)
    device: DeviceEligibilitySettings = Field(default_factory=DeviceEligibilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Used by tests that patch the environment."""
    get_settings.cache_clear()
