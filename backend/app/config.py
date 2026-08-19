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

    #: Section 17: SIG-02 offers another attempt, SIG-03 does not. Three is the count the PM spec's
    #: repeated-failure screen is written around ("3 unsuccessful attempts") and the same ceiling
    #: the handset counts against in `check_session.dart`.
    max_capture_attempts: int = 3

    # Trimming rule is fixed by BUILD_SPEC 4.3 ("discard beyond 1.5 x IQR"), the standard Tukey
    # fence. Exposed as configuration so it is not a literal in the engine.
    iqr_fence_multiplier: float = 1.5

    # A baseline needs at least three accepted calibration sessions (BUILD_SPEC 4.1, enforced as
    # a DB CHECK). Three is the spec's floor: enough to compute a standard deviation with any
    # meaning at all, and it is a floor rather than a recommendation.
    # **1, not 3 — single-point calibration is the product.** Tera calibrates against one
    # validated cuff reading and estimates from PTT thereafter (invariant 1, rewritten 14 August
    # 2026; see docs/decisions.md). Requiring three prior sessions contradicted that: a patient
    # who took their cuff reading on day one could not be calibrated until day two or three, and
    # until then no estimate was produced at all.
    #
    # What is given up, stated plainly: a baseline from one capture carries that capture's noise
    # with no averaging to damp it. Three sessions gave a steadier anchor. The mitigation is that
    # the anchor is only ever an intercept — `pressure_estimate` refuses outright once a reading
    # drifts past `max_ptt_drift_ms` from it — and that recalibration is prompted at four weeks.
    min_calibration_sessions: int = 1

    # Minimum usable beats for a session to yield an estimate.
    #
    # **Source: the ML team's `MIN_PAIRS = 12` in `final_round/ptt/tera_ptt.py`** — "beats needed
    # for a usable median". That is the figure the chain was validated against, and it is the
    # figure the handset's own gate has always used; the reference vectors in
    # `patient/test/fixtures/` are checked against a Python run that accepts at 12.
    #
    # **This was 30, which was a second and stricter gate sitting behind the first.** The stated
    # reasoning was that a 60 s capture at 60 bpm gives ~60 beats, so 30 means half the capture
    # survived — a sound-sounding rule that was never validated and was not the reference's. Its
    # effect was that a capture the ML chain accepts at 12-29 pairs was refused here as
    # `insufficient_beats`, with the patient told to record again for a minute they had already
    # given usably. Half of a 60 bpm capture is also not half of a 48 bpm one, so the rule was
    # harshest on the slowest heart rates.
    #
    # A trimmed mean over 12 beats is noisier than over 30, and that cost is real. It is carried
    # where it belongs: `compute_confidence` scores a 12-beat session low, and the trend it feeds
    # is a direction rather than a claim. Refusing the capture outright was not the conservative
    # choice — it produced no record at all rather than a weak one, which is worse for a product
    # whose value proposition is record completeness.
    #
    # Per-episode override: protocol_params.min_beat_count.
    min_usable_beats: int = 12

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
    # **An absolute beat count, not a multiple of the minimum, and the difference matters.**
    #
    # This was `min_usable_beats * 2.0`. That tied the point at which more beats stop improving
    # the score to the point at which a session is allowed at all — two unrelated facts. Dropping
    # the floor from 30 to 12 would have dragged the saturation point from 60 beats to 24, so a
    # 24-beat capture would score full marks on the beat term where it previously scored 0.4.
    # Lowering a gate is a decision about what to accept; it must not silently become a decision
    # to describe what it accepts more confidently.
    #
    # 60 beats is a full 60-second capture at 60 bpm, which is what the old default worked out to
    # and is the honest reading of "more beats past here do not make the estimate better".
    confidence_beat_saturation_beats: int = 60
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


class PressureEstimateSettings(BaseSettings):
    """PTT -> mmHg, anchored on the patient's own cuff calibration.

    **What this is.** Single-point calibration: one validated cuff reading fixes the offset, and
    a sensitivity coefficient converts a later change in pulse transit time into a change in
    pressure. It is the same approach shipping cuffless products use (Samsung Health Monitor
    calibrates against a cuff and then estimates, requiring recalibration every four weeks), and
    it is a product decision recorded in docs/decisions.md, not a derivation this project claims
    to have validated.

    **What one calibration point can and cannot fix.** It fixes the *intercept* — where this
    patient sits. It cannot fix the *slope*: the sensitivity below comes from population data and
    is not personalised, so the further a reading drifts from the calibration point the less the
    number is worth. That is the dominant error term and it is why `estimate_confidence` falls
    away with distance from the anchor, and why recalibration is prompted rather than optional.

    The relation is linear in delta-PTT, which is the first-order approximation of the
    Moens-Korteweg / Bramwell-Hill relation over the narrow PTT range a resting adult spans.
    """

    model_config = SettingsConfigDict(env_prefix="TERA_PRESSURE_")

    #: mmHg per millisecond of PTT shortening, systolic. Reported sensitivities in PTT-BP studies
    #: cluster around 0.7-1.3 mmHg/ms over the resting range; 0.9 is mid-range. Population-derived
    #: and NOT personalised - see the class docstring.
    systolic_mmhg_per_ms: float = 0.9

    #: Diastolic tracks PTT more weakly than systolic in the same studies, roughly half.
    diastolic_mmhg_per_ms: float = 0.5

    #: Beyond this much drift from the calibration PTT the linear approximation is not defensible
    #: and no number is produced. Invariant 7: withhold rather than guess.
    max_ptt_drift_ms: float = 60.0

    #: Output clamps. An estimate outside these is a signal problem, not a physiological finding,
    #: and is withheld. Matches the cuff plausibility bounds so the two cannot disagree.
    systolic_min_mmhg: int = 70
    systolic_max_mmhg: int = 220
    diastolic_min_mmhg: int = 40
    diastolic_max_mmhg: int = 140

    #: How old a calibration may be before the estimate is withheld and a new cuff reading asked
    #: for. Samsung requires four weeks for the same reason: the slope drifts with vascular tone.
    max_calibration_age_days: int = 28


class PlausibilitySettings(BaseSettings):
    """Server-side payload plausibility (BUILD_SPEC 4.4, defence in depth).

    The quality gate runs on the device. These bounds exist because the backend must not trust
    the client.
    """

    model_config = SettingsConfigDict(env_prefix="TERA_PLAUSIBILITY_")

    # BUILD_SPEC 4.4 states the range explicitly: "PTT values outside 80-400 ms". Consistent with
    # reported pulse transit / pulse arrival times over the proximal arterial path in adults.
    #
    # **The ceiling is 500, not the spec's 400. A deliberate deviation, recorded here and in
    # docs/decisions.md.**
    #
    # The physiology is not in dispute; the fiducials at either end are. The handset marks aortic
    # opening by backtracking to 82% of the envelope rise, which lands *earlier* than the envelope
    # peak, and marks the PPG foot by intersecting tangents, which lands earlier than the argmin.
    # Both corrections are correct — they are what the ML reference specifies and why the port
    # matches it to the microsecond — and both lengthen the interval between them. The quantity
    # this bound measures is therefore systematically larger than the textbook AO-to-foot figure
    # 80-400 was written against.
    #
    # Keeping 400 here while the handset pairs to 500 would be the worst of both: the phone would
    # accept a capture and the server would 422 the whole session on one interval, which is
    # exactly the split that cost us `min_usable_beats`. The two move together or not at all.
    #
    # This does not weaken the defence-in-depth this class exists for. A 500 ms ceiling is still
    # half a cardiac cycle at 60 bpm, so it cannot admit a pair formed across two different beats,
    # and a session mixing true intervals with spurious ones scatters far past the deviation
    # engine's dispersion handling. The floor is unchanged.
    ptt_min_ms: float = 80.0
    ptt_max_ms: float = 500.0

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

    # Source: proposal page 7 — minimum 200 Hz, target 500 Hz, with non-compliant handsets
    # "excluded at onboarding rather than permitted to produce estimates whose error exceeds the
    # signal". The proposal's own measured figure is the argument: jitter is 10.6 ms at 100 Hz
    # against a signal carried in 10–50 ms shifts, so below the floor the timing error is larger
    # than the effect being measured, and no downstream processing recovers that.
    #
    # 200 Hz is therefore a floor, not a target. It is the point below which the method stops
    # meaning anything; a handset sitting just above it is not "qualified", it is usable with a
    # stated caveat. 500 Hz is where the sample interval stops being a leading error term.
    #
    # Consequence worth stating plainly: Android caps sensor delivery at 200 Hz unless the app
    # holds HIGH_SAMPLING_RATE_SENSORS (Android 12+), so **most handsets will land in the
    # provisional band**. Provisional therefore has to read as a normal, workable outcome rather
    # than a warning, and it gates nothing — see test_provisional_status_gates_nothing.
    accel_rate_qualified_hz: float = 500.0
    accel_rate_provisional_hz: float = 200.0

    # Camera frame interval bounds PPG timing resolution: 30 fps is 33 ms per frame, so pulse
    # arrival is interpolated between frames rather than read off directly. 60 fps halves that
    # error, hence the qualified band. Below 30 fps the interpolation is doing more work than
    # the measurement.
    #
    # **The provisional floor is 25, not 30, and the 0.2 fps that forced the change is the whole
    # argument.** A handset delivering a measured 29.8 fps was graded `not_qualified` by a
    # threshold set at exactly 30.0 — but no camera advertising "30 fps" delivers 30.000. Real
    # hardware lands at 29.7-29.97 (the same reason broadcast video is 29.97), so a floor set at
    # the nominal rate rejects every nominal-30 device in existence, which is not a clinical
    # judgement about anything.
    #
    # 25 fps is where the measurement argument actually sits. A resting pulse is 0.7-3 Hz, so
    # Nyquist needs 6 Hz and 25 leaves four times that margin; what frame rate really bounds is
    # the interpolation of the PPG foot between frames, and at 25 fps that is a 40 ms window
    # against transit-time shifts of 10-50 ms. Below about 20 the interpolation dominates the
    # measurement, which is where the real floor belongs.
    camera_fps_qualified: float = 60.0
    camera_fps_provisional: float = 25.0

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


class ReferenceSettings(BaseSettings):
    """The BP reference lifecycle and the monitoring-gap rule (PM spec sections 12 and 27).

    Invariant 10: these are the thresholds that decide when a patient is asked for a fresh cuff
    reading, so they are configuration and each default carries its source.
    """

    model_config = SettingsConfigDict(env_prefix="TERA_REFERENCE_", extra="ignore")

    #: How long a reference stays valid before a refresh is requested. PM spec section 27 states
    #: the monitoring-gap rule in terms of "no sensor check for a while" rather than a number; 14
    #: days is an engineering choice matching the two-week rhythm of the proposal's home-monitoring
    #: schedule, not a validated clinical figure.
    reference_validity_days: int = 14

    #: A gap in *sensor* checks long enough that the trend can no longer be read against the
    #: existing baseline. Section 27's "monitoring gap". Same standing as the figure above.
    monitoring_gap_days: int = 30

    #: Section 12: rest before a reference reading. Stated in the spec's BPREF-01 copy as five
    #: minutes, and surfaced here so the copy and the rule cannot drift apart.
    reference_rest_minutes: int = 5


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

    # ---------------------------------------------------------------- auth endpoint limits
    #
    # These are the brute-force defence, so unlike the ceilings above they are deliberately close
    # to legitimate use, and they are enforced across processes (see app/security/authlimit.py).
    #
    # Login, keyed on the attempted username: a person who has forgotten their password tries a
    # handful of times and then asks the clinic. Ten in fifteen minutes is generous for that and
    # cuts an online guessing attack to roughly 960 attempts a day against one account, which is
    # useless against any password worth the name.
    auth_login_limit_per_username: int = 10
    auth_login_window_seconds: int = 900

    # Login, keyed on client address, and necessarily looser: behind NAT or CGNAT one address is
    # a whole building. Set high enough that a school or clinic sharing an address is not locked
    # out, low enough to bound username-spraying from a single host.
    auth_login_limit_per_address: int = 60
    auth_login_address_window_seconds: int = 900

    # Refresh, keyed on the token family. One family is one login, and a well-behaved client
    # refreshes roughly once per access-token lifetime — four times an hour at a 15-minute TTL.
    # Twenty an hour absorbs retries and clock skew without absorbing a loop.
    auth_refresh_limit_per_family: int = 20
    auth_refresh_window_seconds: int = 3600

    # How far past the family limit is treated as abuse rather than a bug. A broken client
    # retrying a few times over should not lose its session; one that keeps going past this has
    # stopped being plausibly accidental, and the family is revoked.
    auth_refresh_breach_revoke_threshold: int = 20

    auth_refresh_limit_per_address: int = 120
    auth_refresh_address_window_seconds: int = 3600

    # Self-registration, keyed on client address. B2C PIVOT: /v1/auth/register-patient is the
    # only unauthenticated route that *writes*, so it is the only one where an attacker gets rows
    # rather than rejections. A real person signs up once; a handful per address per hour covers a
    # shared connection and a couple of failed attempts, and bounds automated account creation to
    # something a human notices. An engineering choice, not a validated figure.
    auth_register_limit_per_address: int = 5
    auth_register_address_window_seconds: int = 3600


class RhythmModelSettings(BaseSettings):
    """The optional rhythm-anomaly model shipped by the ML team.

    **Off by default, and that is the recommendation, not an oversight.** The handoff is explicit:
    the model powers exactly one field, nothing depends on it, and "a missing flag costs nothing.
    A false 'irregular rhythm' on a healthy volunteer in front of a judge costs a lot."

    The artefact is a 52 MB scikit-learn pickle. A pickle that size is bound to the scikit-learn
    version that wrote it — a different version may refuse to load it, or load it and behave
    subtly differently. Enabling this without pinning that version is how the second failure
    happens silently.
    """

    model_config = SettingsConfigDict(env_prefix="TERA_RHYTHM_")

    #: Opt-in. Nothing loads, and nothing is imported, while this is false.
    enabled: bool = False

    #: Explicit path wins over the search order in ``app/ml/registry.py``.
    path: str | None = None

    #: Fallback used only when the bundle ships no ``op_threshold``. The handoff calls this out:
    #: "If op_threshold is absent, tera_ptt prints a loud warning and falls back to 0.5, which is
    #: not the threshold the model was tuned at." The registry logs at WARNING when it is used.
    fallback_op_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class LlmInsightSettings(BaseSettings):
    """The optional LLM-generated commentary offered alongside the deterministic insight.

    **Off by default, and consent-gated even when configured.** The rule engine in
    `app/services/insight.py` is the insight — deterministic, tested, invariant-covered, and
    unaffected by anything in this class. This is a second, clearly-labelled paragraph a patient
    may ask for and may decline; declining, or leaving it unconfigured, changes nothing about the
    response the deterministic engine already returns.

    Nothing here relaxes invariant 6. Every string this produces is checked against
    `language.find_forbidden_language` before it reaches a response; a hit is discarded, not
    edited, because a filter that "fixes" clinical language is a second, unreviewed source of
    clinical language.
    """

    model_config = SettingsConfigDict(env_prefix="TERA_LLM_")

    #: No key, no calls — `enabled` is computed, not a second flag to forget to flip.
    api_key: str | None = None
    api_url: str = "https://integrate.api.nvidia.com/v1/chat/completions"
    model: str = "meta/llama-3.1-8b-instruct"
    #: Generous enough for a real API round trip, short enough that a hung provider does not
    #: leave a patient staring at PROC-01 for the rest of the demo.
    timeout_seconds: float = 12.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


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
    reference: ReferenceSettings = Field(default_factory=ReferenceSettings)
    rhythm_model: RhythmModelSettings = Field(default_factory=RhythmModelSettings)
    llm_insight: LlmInsightSettings = Field(default_factory=LlmInsightSettings)
    pressure_estimate: PressureEstimateSettings = Field(
        default_factory=PressureEstimateSettings
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Used by tests that patch the environment."""
    get_settings.cache_clear()
