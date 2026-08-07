"""``tera-seed-demo`` — build one browsable four-week episode.

BUILD_SPEC 4.6: the persona is a 52-year-old with recently intensified treatment. The episode
contains three calibration sessions, roughly thirty routine sessions with a plausible slow PTT
drift and day-to-day scatter, several rejected sessions across different reasons, a handful of
cuff readings, medication events, one symptom event, one deviation -> repeat -> cuff-confirmation
sequence, and one recalibration.

**Invariant 9 governs every row this module writes.** Every record is marked ``synthetic=True``,
the API surfaces that flag on every response, and the CLI says so on the way in and on the way
out. None of these numbers came from a person. The PTT values are generated from a documented
model chosen to exercise the deviation engine — they are not measurements, not derived from
measurements, and must never be cited as evidence of anything.

The session values go through the real ingest path (``app.services.ingest.submit``), so the
seeded episode exercises the same plausibility gate, calibration resolution and deviation engine
as a real device. A seeder that wrote estimates directly would prove nothing.
"""

from __future__ import annotations

import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import typer
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.models import (
    AppUser,
    Calibration,
    CameraHardwareLevel,
    CuffReading,
    CuffSource,
    DeviceProfile,
    MedicationEvent,
    MonitoringEpisode,
    Patient,
    Posture,
    QualifiedStatus,
    RejectionReason,
    SessionStatus,
    SymptomEvent,
    TimestampSource,
    UserRole,
)
from app.schemas.session import SessionQuality, SessionSubmit
from app.security.passwords import hash_password
from app.services import calibration as calibration_service
from app.services import ingest
from app.services.eligibility import evaluate_device

app = typer.Typer(add_completion=False, help="Seed a synthetic demonstration episode.")

# --------------------------------------------------------------------------- the model
#
# All of the numbers below are chosen to exercise the engine, not observed from anyone.
#
# Baseline PTT around 250 ms is the order of magnitude reported for proximal arterial pulse
# transit in adults. The rest — the drift, the scatter, the beat-to-beat variation — are
# generated so the demo shows a stable stretch, one genuine deviation sequence, and a
# recalibration. They are illustrative.

SEED = 20260807  # fixed so the demo is identical on every machine

BASELINE_CALIBRATION_PTTS = (246.0, 252.0, 258.0)  # mean 252.0, sd 6.0 -> k=2 threshold 12 ms
STABLE_TARGET_MS = 252.0
STABLE_SCATTER_MS = 3.5

#: The deviation sequence: day 18 reads outside the usual range, day 19 repeats it, which makes
#: it persistent and triggers the cuff request. Both are well beyond the 12 ms threshold.
DEVIATION_DAY_ONE_MS = 238.0
DEVIATION_DAY_TWO_MS = 236.0

#: After the confirmed cuff reading the patient settles at a shorter PTT. Recalibration on day
#: 21 re-anchors the baseline there, which is the whole point of versioned calibration.
POST_EVENT_CALIBRATION_PTTS = (238.0, 248.0, 245.0)  # mean 243.67, sd 5.13 -> threshold 10.3 ms
POST_EVENT_TARGET_MS = 244.0
POST_EVENT_SCATTER_MS = 3.0

BEAT_SCATTER_MS = 8.0  # beat-to-beat variation within one capture
BEATS_PER_SESSION = (52, 68)

EPISODE_DAYS = 28
MODEL_VERSION = "tera-scg-ppg-0.1.0-synthetic"

# --------------------------------------------------------------------------- session yield
#
# What fraction of attempts the device gate throws away (invariant 3 — they are all retained).
#
# The MVP target of ~80% usable is stated for *controlled seated conditions*. This episode is
# not that: it is a 52-year-old self-administering at home, unsupervised, holding a phone
# against their sternum with one hand and a fingertip on the lens with the other, twice a day
# for four weeks. Assuming the controlled-conditions figure transfers to that setting would be
# assuming away the hardest part of the problem, and a demo showing near-perfect acquisition
# would invite exactly the question we could not answer.
#
# So the default is deliberately pessimistic relative to the target. It is a **modelling
# assumption for a demonstration**, not a measured yield — no acquisition study has been run
# (invariant 9). Override with --rejection-rate to show a different scenario.
DEFAULT_REJECTION_RATE = 0.32

#: Relative frequency of each rejection reason, weighted toward the two failure modes that
#: dominate unsupervised home capture: the patient moving, and the sensor/lens contact being
#: wrong. The rarer entries are device- and environment-level faults.
REJECTION_WEIGHTS: tuple[tuple[RejectionReason, float], ...] = (
    # Motion — the patient shifts, coughs, adjusts their grip mid-capture.
    (RejectionReason.EXCESSIVE_MOTION, 0.30),
    (RejectionReason.POSTURE_UNSTABLE, 0.12),
    # Placement — fingertip not fully covering the lens, or the phone sliding on the sternum.
    (RejectionReason.POOR_SIGNAL_QUALITY, 0.26),
    (RejectionReason.INSUFFICIENT_BEATS, 0.14),
    # Device and environment.
    (RejectionReason.SENSOR_RATE_BELOW_QUALIFIED, 0.07),
    (RejectionReason.TORCH_UNAVAILABLE, 0.04),
    (RejectionReason.CLOCK_UNSTABLE, 0.02),
    (RejectionReason.USER_ABORTED, 0.05),
)


@dataclass
class SeedContext:
    db: Session
    rng: random.Random
    day_zero: datetime
    patient: Patient
    episode: MonitoringEpisode
    device_profile: DeviceProfile
    settings: object
    rejection_rate: float = DEFAULT_REJECTION_RATE
    accepted_count: int = 0
    rejected_count: int = 0
    reasons_seen: set = field(default_factory=set)

    def at(self, day: int, hour: int = 8, minute: int = 0) -> datetime:
        return self.day_zero + timedelta(days=day, hours=hour, minutes=minute)

    def pick_rejection_reason(self) -> RejectionReason:
        reasons = [reason for reason, _ in REJECTION_WEIGHTS]
        weights = [weight for _, weight in REJECTION_WEIGHTS]
        return self.rng.choices(reasons, weights=weights, k=1)[0]


@app.command()
def main(
    reset: bool = typer.Option(
        False,
        "--reset",
        help="Truncate all tables first. Development only — the append-only triggers block "
        "row deletion by design, so this uses TRUNCATE.",
    ),
    rejection_rate: float = typer.Option(
        DEFAULT_REJECTION_RATE,
        "--rejection-rate",
        min=0.0,
        max=0.9,
        help="Fraction of capture attempts the device gate rejects. The default models "
        "unsupervised home self-administration, which is a harder setting than the "
        "controlled-conditions yield target. A modelling assumption, not a measured value.",
    ),
) -> None:
    """Seed one synthetic four-week episode."""
    settings = get_settings()

    typer.echo("Seeding SYNTHETIC demonstration data.")
    typer.echo("Every row written here is marked synthetic=true and is not a real measurement.")
    typer.echo("")

    with session_scope() as db:
        if reset:
            _truncate_everything(db)
            typer.echo("  reset: all tables truncated")

        ctx = _bootstrap(db, settings, rejection_rate=rejection_rate)
        typer.echo(f"  patient   {ctx.patient.pseudonym} (synthetic)")
        typer.echo(f"  episode   {ctx.episode.id}")
        typer.echo(f"  device    {ctx.device_profile.model} -> "
                   f"{ctx.device_profile.qualified_status.value}")

        enrolment_cuff = _cuff_reading(ctx, day=0, hour=9, systolic=156, diastolic=96, pulse=78)
        typer.echo("  cuff      enrolment reading recorded")

        first_calibration = _seed_calibration_phase(ctx, enrolment_cuff)
        typer.echo(
            f"  calib     C1 established from 3 sessions "
            f"(baseline {first_calibration.baseline_mean_ms:.1f} +/- "
            f"{first_calibration.baseline_sd_ms:.1f} ms)"
        )

        _seed_routine_phase(ctx)
        _seed_medication_events(ctx)
        _seed_symptom_event(ctx)
        typer.echo("  events    medication log + 1 symptom report")

        confirmation_cuff = _seed_deviation_sequence(ctx)
        typer.echo("  deviation possible -> persistent -> cuff confirmation recorded")

        second_calibration = _seed_recalibration(ctx, confirmation_cuff)
        typer.echo(
            f"  recalib   C2 established, C1 superseded "
            f"(baseline {second_calibration.baseline_mean_ms:.1f} +/- "
            f"{second_calibration.baseline_sd_ms:.1f} ms)"
        )

        _seed_post_recalibration_phase(ctx)
        _cuff_reading(ctx, day=26, hour=9, systolic=142, diastolic=88, pulse=72)
        typer.echo("  cuff      scheduled readings recorded")

        topped_up = _top_up_rejections(ctx)

        total = ctx.accepted_count + ctx.rejected_count
        achieved = ctx.rejected_count / total if total else 0.0
        typer.echo(
            f"  sessions  {total} attempts: {ctx.accepted_count} accepted, "
            f"{ctx.rejected_count} rejected ({achieved:.0%} rejected, "
            f"{1 - achieved:.0%} usable)"
        )
        typer.echo(
            f"  yield     target {rejection_rate:.0%} rejected; "
            f"{len(ctx.reasons_seen)}/{len(REJECTION_WEIGHTS)} reasons represented"
            + (f"; {topped_up} added to reach the target and cover rare reasons" if topped_up else "")
        )

    typer.echo("")
    typer.echo("Done. This episode is SYNTHETIC. The API flags every row accordingly.")
    typer.echo(
        "The rejection rate is a modelling assumption for unsupervised home use, not a "
        "measured acquisition yield. No acquisition study has been run."
    )
    typer.echo(f"  patient login    : {_PATIENT_SUBJECT}")
    typer.echo(f"  clinician login  : {_CLINICIAN_SUBJECT}")
    typer.echo("  passwords come from TERA_DEMO_*_PASSWORD in your .env")


# --------------------------------------------------------------------------- bootstrap

_PATIENT_SUBJECT = "demo.patient@tera.invalid"
_CLINICIAN_SUBJECT = "demo.clinician@tera.invalid"
_CLINIC_ID = "CLINIC-DEMO-01"


def _bootstrap(
    db: Session, settings, rejection_rate: float = DEFAULT_REJECTION_RATE
) -> SeedContext:
    """Create the patient, users, episode and device profile."""
    now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    day_zero = now - timedelta(days=EPISODE_DAYS)

    suffix = uuid.uuid4().hex[:6]
    patient = Patient(
        pseudonym=f"TERA-DEMO-{suffix}",
        clinic_id=_CLINIC_ID,
        enrolled_at=day_zero,
        synthetic=True,
    )
    db.add(patient)
    db.flush()

    clinician = _upsert_user(
        db,
        subject=_CLINICIAN_SUBJECT,
        role=UserRole.CLINICIAN,
        password=settings.demo_clinician_password,
        patient_id=None,
    )
    _upsert_user(
        db,
        subject=_PATIENT_SUBJECT,
        role=UserRole.PATIENT,
        password=settings.demo_patient_password,
        patient_id=patient.id,
    )

    episode = MonitoringEpisode(
        patient_id=patient.id,
        reviewing_clinician_id=clinician.id,
        started_at=day_zero,
        ended_at=None,  # still open, so the demo looks live
        protocol_params={
            "cuff_schedule": {
                "description": "Upper-arm cuff at enrolment, then weekly, plus on request.",
                "interval_days": 7,
            },
            "deviation_k": 2,
            "min_beat_count": 30,
            "persistence_window_hours": 48,
        },
        synthetic=True,
    )
    db.add(episode)
    db.flush()

    # Measured-looking values for a mid-range handset. Invariant 9: these are illustrative
    # numbers for a fictional device, not benchmark results from real hardware.
    accel_rate_hz, camera_fps = 204.8, 60.4
    verdict = evaluate_device(
        accel_rate_hz=accel_rate_hz,
        camera_fps=camera_fps,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.4,
        settings=settings.device,
    )
    device_profile = DeviceProfile(
        patient_id=patient.id,
        model="Synthetic Reference Handset (demo)",
        os_version="Android 14",
        accel_rate_hz=accel_rate_hz,
        camera_fps=camera_fps,
        camera_hw_level=CameraHardwareLevel.FULL,
        manual_sensor=True,
        timestamp_source=TimestampSource.REALTIME,
        clock_offset_sd_ms=1.4,
        qualified_status=verdict.status,
        submitted_at=day_zero,
        synthetic=True,
    )
    db.add(device_profile)
    db.flush()

    return SeedContext(
        db=db,
        rng=random.Random(SEED),
        day_zero=day_zero,
        patient=patient,
        episode=episode,
        device_profile=device_profile,
        settings=settings,
        rejection_rate=rejection_rate,
    )


def _upsert_user(
    db: Session, *, subject: str, role: UserRole, password: str, patient_id: uuid.UUID | None
) -> AppUser:
    """Create the demo account, or re-point an existing one at the new patient."""
    from sqlalchemy import select

    existing = db.execute(select(AppUser).where(AppUser.subject == subject)).scalar_one_or_none()
    if existing is not None:
        # app_user is not a clinical table, so re-seeding may repoint it at the new episode.
        existing.patient_id = patient_id
        existing.password_hash = hash_password(password)
        db.flush()
        return existing

    user = AppUser(
        subject=subject,
        password_hash=hash_password(password),
        role=role,
        clinic_id=_CLINIC_ID,
        patient_id=patient_id,
        synthetic=True,
    )
    db.add(user)
    db.flush()
    return user


# --------------------------------------------------------------------------- phases


def _seed_calibration_phase(ctx: SeedContext, reference_cuff: CuffReading) -> Calibration:
    """Days 0-2: three calibration sessions, then the first calibration."""
    session_ids = []
    for index, target in enumerate(BASELINE_CALIBRATION_PTTS):
        result = _submit_session(ctx, day=index, hour=8, target_ptt_ms=target)
        session_ids.append(result.session.id)

    established = calibration_service.establish(
        ctx.db,
        patient_id=ctx.patient.id,
        device_profile_id=ctx.device_profile.id,
        reference_cuff_reading_id=reference_cuff.id,
        session_ids=session_ids,
        settings=ctx.settings,
        synthetic=True,
        now=ctx.at(2, hour=12),
    )
    return established.calibration


def _attempt_session(ctx: SeedContext, *, day: int, hour: int, target_ptt_ms: float) -> None:
    """One scheduled measurement, including however many failed attempts preceded it.

    A patient whose capture is rejected tries again — so failures cluster around a scheduled
    slot rather than replacing it. The geometric draw makes the long-run rejected fraction equal
    ``rejection_rate``: expected failures per success is r/(1-r).

    Each retry is timestamped a few minutes earlier so the record reads in the order it happened.
    """
    retries = 0
    while ctx.rng.random() < ctx.rejection_rate and retries < 4:
        retries += 1

    for attempt in range(retries, 0, -1):
        reason = ctx.pick_rejection_reason()
        _submit_rejected(
            ctx, day=day, hour=hour, minute=-3 * attempt, reason=reason
        )

    _submit_session(ctx, day=day, hour=hour, target_ptt_ms=target_ptt_ms)


def _seed_routine_phase(ctx: SeedContext) -> None:
    """Days 3-17: routine sessions with day-to-day scatter, plus the attempts that failed."""
    for day in range(3, 18):
        target = STABLE_TARGET_MS + ctx.rng.gauss(0, STABLE_SCATTER_MS)
        _attempt_session(ctx, day=day, hour=8, target_ptt_ms=target)

        # Twice-daily on alternate days, which is what the protocol asks for.
        if day % 2 == 1:
            target_pm = STABLE_TARGET_MS + ctx.rng.gauss(0, STABLE_SCATTER_MS)
            _attempt_session(ctx, day=day, hour=20, target_ptt_ms=target_pm)

        if day == 7:
            _cuff_reading(ctx, day=7, hour=9, systolic=150, diastolic=94, pulse=76)
        if day == 14:
            _cuff_reading(ctx, day=14, hour=9, systolic=147, diastolic=91, pulse=74)


def _seed_deviation_sequence(ctx: SeedContext) -> CuffReading:
    """Days 18-19: deviation, repeat, cuff confirmation.

    The first deviating session must produce ``possible`` and *not* request a cuff; the repeat
    inside the persistence window makes it ``persistent``, which does. That ordering is the
    behaviour BUILD_SPEC 4.3 requires and the reason both sessions are here.
    """
    _submit_session(ctx, day=18, hour=8, target_ptt_ms=DEVIATION_DAY_ONE_MS)
    _submit_session(ctx, day=19, hour=8, target_ptt_ms=DEVIATION_DAY_TWO_MS)
    return _cuff_reading(ctx, day=19, hour=11, systolic=162, diastolic=99, pulse=81)


def _seed_recalibration(ctx: SeedContext, reference_cuff: CuffReading) -> Calibration:
    """Days 20-21: three fresh sessions, then a new calibration superseding the first."""
    session_ids = []
    schedule = ((20, 8), (20, 20), (21, 8))
    for (day, hour), target in zip(schedule, POST_EVENT_CALIBRATION_PTTS):
        result = _submit_session(ctx, day=day, hour=hour, target_ptt_ms=target)
        session_ids.append(result.session.id)

    established = calibration_service.establish(
        ctx.db,
        patient_id=ctx.patient.id,
        device_profile_id=ctx.device_profile.id,
        reference_cuff_reading_id=reference_cuff.id,
        session_ids=session_ids,
        settings=ctx.settings,
        synthetic=True,
        now=ctx.at(21, hour=12),
    )
    return established.calibration


def _seed_post_recalibration_phase(ctx: SeedContext) -> None:
    """Days 22-27: sessions read against the new baseline."""
    for day in range(22, EPISODE_DAYS):
        target = POST_EVENT_TARGET_MS + ctx.rng.gauss(0, POST_EVENT_SCATTER_MS)
        _attempt_session(ctx, day=day, hour=8, target_ptt_ms=target)


def _top_up_rejections(ctx: SeedContext) -> int:
    """Bring the episode up to the target rejection rate, and cover every reason.

    The per-attempt retry draw is stochastic, so on any given seed the achieved rate lands a few
    points either side of the target — this seed produced 23% against a target of 32%. A demo
    whose headline yield figure moves depending on the random seed is not a demo you want to
    stand behind, so the shortfall is made up deterministically here.

    Two things happen, in order:

    1. Any rejection reason the draw missed gets one session. The rare reasons sit at 2-5%
       weight and a four-week episode will usually miss one or two, but the clinician summary's
       per-reason breakdown needs to be shown working.
    2. Weighted-random rejections are added until ``rejected / total >= rejection_rate``.

    Both are spread across the episode rather than bunched on one day. The consequence — worth
    being explicit about — is that the reason *distribution* is not purely ``REJECTION_WEIGHTS``,
    and the achieved rate is engineered rather than emergent. All of it is synthetic, and the
    CLI reports both numbers so nothing here reads as a measured spread.
    """
    added = 0

    missing = [reason for reason, _ in REJECTION_WEIGHTS if reason not in ctx.reasons_seen]
    for reason in missing:
        day, hour, minute = _spare_slot(ctx, added)
        _submit_rejected(ctx, day=day, hour=hour, minute=minute, reason=reason)
        added += 1

    while ctx.rejected_count / (ctx.accepted_count + ctx.rejected_count) < ctx.rejection_rate:
        day, hour, minute = _spare_slot(ctx, added)
        _submit_rejected(ctx, day=day, hour=hour, minute=minute, reason=ctx.pick_rejection_reason())
        added += 1
        if added > 60:  # defensive: rejection_rate is capped at 0.9, so this cannot be hit
            break

    return added


def _spare_slot(ctx: SeedContext, index: int) -> tuple[int, int, int]:
    """Spread topped-up rejections across the episode instead of bunching them on one day."""
    day = 3 + (index * 5) % (EPISODE_DAYS - 4)
    hour = 10 + (index % 3) * 4
    return day, hour, (index * 11) % 60


def _seed_medication_events(ctx: SeedContext) -> None:
    """A medication log. Invariant 6 — recorded, never responded to with advice."""
    for day in range(0, EPISODE_DAYS):
        # A realistic log has gaps in it.
        if day % 7 == 5:
            continue
        ctx.db.add(
            MedicationEvent(
                episode_id=ctx.episode.id,
                occurred_at=ctx.at(day, hour=7, minute=30),
                payload={
                    "medication": "antihypertensive (synthetic placeholder)",
                    "taken": True,
                    "schedule": "morning",
                },
                recorded_at=ctx.at(day, hour=7, minute=32),
                synthetic=True,
            )
        )


def _seed_symptom_event(ctx: SeedContext) -> None:
    """One non-red-flag symptom report."""
    ctx.db.add(
        SymptomEvent(
            episode_id=ctx.episode.id,
            occurred_at=ctx.at(12, hour=16),
            payload={
                "symptom": "mild headache",
                "severity": "mild",
                "red_flag": False,
                "note": "synthetic demonstration record",
            },
            recorded_at=ctx.at(12, hour=16, minute=5),
            synthetic=True,
        )
    )


# --------------------------------------------------------------------------- primitives


def _cuff_reading(
    ctx: SeedContext, *, day: int, hour: int, systolic: int, diastolic: int, pulse: int
) -> CuffReading:
    """Record a synthetic cuff reading.

    These mmHg values are invented for the demonstration. They are marked synthetic in the
    database and in every API response that carries them (invariant 9).
    """
    taken_at = ctx.at(day, hour=hour)
    reading = CuffReading(
        episode_id=ctx.episode.id,
        systolic_mmhg=systolic,
        diastolic_mmhg=diastolic,
        pulse_bpm=pulse,
        source=CuffSource.MANUAL_ENTRY,
        taken_at=taken_at,
        user_confirmed_at=taken_at + timedelta(minutes=2),
        recorded_at=taken_at + timedelta(minutes=2),
        synthetic=True,
    )
    ctx.db.add(reading)
    ctx.db.flush()
    return reading


def _beats_for(ctx: SeedContext, target_ptt_ms: float, n_beats: int) -> list[float]:
    """Generate per-beat intervals whose trimmed mean lands near ``target_ptt_ms``.

    Beat-to-beat variation is real — respiration alone moves transit time — so a flat array
    would not exercise the IQR trim the engine depends on.
    """
    return [round(ctx.rng.gauss(target_ptt_ms, BEAT_SCATTER_MS), 2) for _ in range(n_beats)]


def _submit_session(ctx: SeedContext, *, day: int, hour: int, target_ptt_ms: float):
    """Push one accepted session through the real ingest path."""
    started_at = ctx.at(day, hour=hour)
    n_beats_total = ctx.rng.randint(*BEATS_PER_SESSION)
    # A few beats always fail the on-device gate; a capture where every beat is usable is not
    # a realistic demonstration of anything.
    n_beats_usable = n_beats_total - ctx.rng.randint(2, 8)
    ptt_ms = _beats_for(ctx, target_ptt_ms, n_beats_usable)

    payload = SessionSubmit(
        session_id=uuid.uuid4(),
        episode_id=ctx.episode.id,
        device_profile_id=ctx.device_profile.id,
        model_version=MODEL_VERSION,
        started_at=started_at,
        posture=Posture.SEATED,
        status=SessionStatus.COMPLETED,
        rejection_reason=None,
        n_beats_total=n_beats_total,
        n_beats_usable=n_beats_usable,
        ptt_ms=ptt_ms,
        quality=SessionQuality(
            accel_rate_hz=round(ctx.rng.uniform(198.0, 206.0), 1),
            camera_fps=round(ctx.rng.uniform(57.0, 60.0), 1),
            dropped_frame_pct=round(ctx.rng.uniform(0.0, 2.5), 2),
            snr_db=round(ctx.rng.uniform(12.0, 19.0), 1),
            motion_index=round(ctx.rng.uniform(0.02, 0.18), 3),
            clock_offset_ms=round(ctx.rng.gauss(0, 1.4), 2),
        ),
        synthetic=True,
    )

    ctx.accepted_count += 1
    return ingest.submit(
        ctx.db,
        payload=payload,
        episode=ctx.episode,
        device_profile=ctx.device_profile,
        settings=ctx.settings,
        received_at=started_at + timedelta(minutes=3),
    )


def _submit_rejected(
    ctx: SeedContext, *, day: int, reason: RejectionReason, hour: int = 12, minute: int = 0
):
    """Push one rejected session through the real ingest path (invariant 3)."""
    started_at = ctx.at(day, hour=hour, minute=minute)
    n_beats_total = ctx.rng.randint(8, 30)

    # Each reason implies a different failure shape; a rejected session that looked exactly
    # like a good one would not demonstrate anything.
    if reason is RejectionReason.INSUFFICIENT_BEATS:
        n_beats_usable = ctx.rng.randint(4, 12)
        quality_overrides = {}
    elif reason is RejectionReason.EXCESSIVE_MOTION:
        n_beats_usable = ctx.rng.randint(2, 8)
        quality_overrides = {"motion_index": 0.87}
    elif reason is RejectionReason.POSTURE_UNSTABLE:
        n_beats_usable = ctx.rng.randint(3, 14)
        quality_overrides = {"motion_index": 0.61}
    elif reason is RejectionReason.POOR_SIGNAL_QUALITY:
        n_beats_usable = ctx.rng.randint(0, 5)
        quality_overrides = {"snr_db": 1.4}
    elif reason is RejectionReason.SENSOR_RATE_BELOW_QUALIFIED:
        n_beats_usable = ctx.rng.randint(5, 15)
        quality_overrides = {"accel_rate_hz": 96.0, "camera_fps": 24.0}
    elif reason is RejectionReason.TORCH_UNAVAILABLE:
        n_beats_usable = 0
        quality_overrides = {"snr_db": -8.0, "camera_fps": 30.0}
    elif reason is RejectionReason.CLOCK_UNSTABLE:
        n_beats_usable = ctx.rng.randint(6, 18)
        quality_overrides = {"clock_offset_ms": 41.0}
    else:  # USER_ABORTED
        n_beats_usable = ctx.rng.randint(0, 6)
        quality_overrides = {}

    n_beats_usable = min(n_beats_usable, n_beats_total)
    quality_values = {
        "accel_rate_hz": round(ctx.rng.uniform(198.0, 206.0), 1),
        "camera_fps": round(ctx.rng.uniform(57.0, 60.0), 1),
        "dropped_frame_pct": round(ctx.rng.uniform(0.0, 6.0), 2),
        "snr_db": round(ctx.rng.uniform(3.0, 9.0), 1),
        "motion_index": round(ctx.rng.uniform(0.2, 0.5), 3),
        "clock_offset_ms": round(ctx.rng.gauss(0, 2.0), 2),
    }
    quality_values.update(quality_overrides)

    payload = SessionSubmit(
        session_id=uuid.uuid4(),
        episode_id=ctx.episode.id,
        device_profile_id=ctx.device_profile.id,
        model_version=MODEL_VERSION,
        started_at=started_at,
        posture=Posture.SEATED,
        status=SessionStatus.REJECTED,
        rejection_reason=reason,
        n_beats_total=n_beats_total,
        n_beats_usable=n_beats_usable,
        ptt_ms=_beats_for(ctx, STABLE_TARGET_MS, n_beats_usable),
        quality=SessionQuality(**quality_values),
        synthetic=True,
    )

    ctx.rejected_count += 1
    ctx.reasons_seen.add(reason)
    return ingest.submit(
        ctx.db,
        payload=payload,
        episode=ctx.episode,
        device_profile=ctx.device_profile,
        settings=ctx.settings,
        received_at=started_at + timedelta(minutes=2),
    )


def _truncate_everything(db: Session) -> None:
    """Development-only reset.

    The append-only triggers block DELETE by design (invariant 5). TRUNCATE is not a row-level
    operation so it is not caught by them, which is exactly why this is not exposed over HTTP
    and never runs outside a developer's own machine.
    """
    tables = [
        "calibration_source_session", "trend_estimate", "measurement_session",
        "clinician_summary", "medication_event", "symptom_event", "red_flag_event",
        "calibration", "cuff_reading", "monitoring_episode", "device_profile",
        "app_user", "patient", "session_nonce", "audit_log",
    ]
    db.execute(text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE"))


def entrypoint() -> None:
    """Console-script entry point."""
    try:
        app()
    except Exception as exc:  # pragma: no cover - CLI surface
        typer.secho(f"seed-demo failed: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
