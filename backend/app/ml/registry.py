"""Loading the rhythm-anomaly model, once, and never at the cost of the app starting.

The pattern comes from the team's previous backend (`JantungSinyal-Backend/ml_service.py`):
prioritised path resolution with an environment override, and a defensive read of the bundle that
tolerates both artefact schemas. Three things are deliberately different, and each is a failure
mode that backend actually has.

**Loading is lazy, not at import.** `ml_service.py` runs `_load_model()` at module scope, so a
missing 52 MB file is an ``ImportError`` that takes down the whole application and every test that
imports anything near it. Here the artefact is read on first use, behind a lock, and a failure
becomes an *unavailable state* rather than an exception.

**It is off by default.** ``TERA_RHYTHM_ENABLED`` must be set. The ML handoff is unambiguous: the
model powers exactly one field, nothing else depends on it, and "a missing flag costs nothing. A
false 'irregular rhythm' on a healthy volunteer in front of a judge costs a lot."

**joblib and scikit-learn are imported inside the function.** Neither is a dependency of this
backend, and the test suite has to keep running on a machine that has never installed a scientific
stack. An optional model must not become a mandatory import.

Nothing here interprets a signal. This module answers one question — is a usable model loaded —
and hands back the estimator with the operating point it was tuned at.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.logging_config import get_logger

log = get_logger(__name__)

#: The feature order the model was trained on. Wrong order, wrong answer, no error — so it is
#: checked against the bundle rather than assumed. From the ML handoff's contract table
#: (`ml/MODEL_HANDOFF.md` section 3), which calls it "WAJIB sama persis".
HRV_FEATURE_NAMES: tuple[str, ...] = (
    "mean_hr_bpm",
    "mean_rr_ms",
    "sdnn_ms",
    "rmssd_ms",
    "rr_cv",
    "min_rr_ms",
    "max_rr_ms",
    "pct_long_rr",
    "longest_brady_run_s",
    "hr_slope",
)

_ARTEFACT_FILENAME = "jantungsinyal_bcg_anomaly_rf.joblib"
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_DIR = _BACKEND_DIR.parent


class RhythmModelState(str, Enum):
    """Why the model is or is not usable. Every value is reportable; none is an error."""

    READY = "ready"
    #: ``TERA_RHYTHM_ENABLED`` is false. The default, and the recommended demo-day setting.
    DISABLED = "disabled"
    #: Enabled, but no artefact at any candidate path.
    NOT_FOUND = "not_found"
    #: Enabled and present, but joblib or scikit-learn is not installed.
    RUNTIME_MISSING = "runtime_missing"
    #: Enabled and present, and the read failed — most often a pickle written by a different
    #: scikit-learn version.
    LOAD_FAILED = "load_failed"


@dataclass(frozen=True)
class LoadedRhythmModel:
    """The artefact, or the reason there is not one.

    ``estimator`` is ``None`` in every state except :attr:`RhythmModelState.READY`, so a caller
    that forgets to check :attr:`is_ready` gets an obvious failure at the point of misuse rather
    than a silent default.
    """

    state: RhythmModelState
    estimator: Any | None = None
    feature_names: tuple[str, ...] = HRV_FEATURE_NAMES
    op_threshold: float = 0.5
    #: True when the bundle shipped no operating point and the configured fallback is in use.
    op_threshold_is_fallback: bool = False
    version: str = "unknown"
    source_path: Path | None = None
    detail: str | None = None

    @property
    def is_ready(self) -> bool:
        return self.state is RhythmModelState.READY and self.estimator is not None


def _candidate_paths(settings: Settings) -> list[Path]:
    """Where to look, most explicit first.

    The artefacts are 52 MB and 37 MB and are **not** in version control, so a path that works on
    one machine will not work on another without the env override or a local copy. Recorded in
    `docs/decisions.md` rather than papered over with a checked-in binary.
    """
    candidates: list[Path] = []

    configured = settings.rhythm_model.path
    if configured:
        candidates.append(Path(configured))

    # Self-contained copy inside the backend image, preferred for Docker.
    candidates.append(_BACKEND_DIR / "ml_models" / _ARTEFACT_FILENAME)
    # The handoff folder in the repo, which is where it lands today.
    candidates.append(_REPO_DIR / "ml" / _ARTEFACT_FILENAME)

    return candidates


def _read_bundle(artefact: Any, settings: Settings, path: Path) -> LoadedRhythmModel:
    """Normalise either bundle schema into one shape.

    The ML team has shipped two. The neonatal build keys the order as ``feature_names``, the adult
    BCG build as ``features``, and only the second carries ``op_threshold``. A bare estimator with
    no wrapper is tolerated too.
    """
    if not isinstance(artefact, dict):
        log.warning("rhythm_model_bare_estimator", extra={"model_path": str(path)})
        return LoadedRhythmModel(
            state=RhythmModelState.READY,
            estimator=artefact,
            op_threshold=settings.rhythm_model.fallback_op_threshold,
            op_threshold_is_fallback=True,
            source_path=path,
            detail="artefact was a bare estimator; feature order and threshold assumed",
        )

    estimator = artefact.get("model")
    if estimator is None:
        return LoadedRhythmModel(
            state=RhythmModelState.LOAD_FAILED,
            source_path=path,
            detail="bundle has no 'model' key",
        )

    names = artefact.get("feature_names") or artefact.get("features") or HRV_FEATURE_NAMES
    feature_names = tuple(str(n) for n in names)

    if len(feature_names) != len(HRV_FEATURE_NAMES):
        return LoadedRhythmModel(
            state=RhythmModelState.LOAD_FAILED,
            source_path=path,
            detail=(
                f"bundle declares {len(feature_names)} features, this build computes "
                f"{len(HRV_FEATURE_NAMES)}"
            ),
        )

    shipped_threshold = artefact.get("op_threshold")
    if shipped_threshold is None:
        # Called out explicitly in the handoff: 0.5 is *not* the threshold the model was tuned at,
        # and the gap is large — the adult bundle ships about 0.10.
        log.warning(
            "rhythm_model_op_threshold_missing",
            extra={
                "model_path": str(path),
                "fallback": settings.rhythm_model.fallback_op_threshold,
            },
        )
        op_threshold = settings.rhythm_model.fallback_op_threshold
        is_fallback = True
    else:
        op_threshold = float(shipped_threshold)
        is_fallback = False

    return LoadedRhythmModel(
        state=RhythmModelState.READY,
        estimator=estimator,
        feature_names=feature_names,
        op_threshold=op_threshold,
        op_threshold_is_fallback=is_fallback,
        version=str(artefact.get("version", "unknown")),
        source_path=path,
    )


def _load(settings: Settings) -> LoadedRhythmModel:
    if not settings.rhythm_model.enabled:
        return LoadedRhythmModel(
            state=RhythmModelState.DISABLED,
            detail="TERA_RHYTHM_ENABLED is false; nothing was loaded or imported",
        )

    candidates = _candidate_paths(settings)
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        log.warning(
            "rhythm_model_not_found", extra={"candidates": [str(c) for c in candidates]}
        )
        return LoadedRhythmModel(
            state=RhythmModelState.NOT_FOUND,
            detail="no artefact at: " + ", ".join(str(c) for c in candidates),
        )

    try:
        import joblib
    except ImportError as exc:
        log.warning("rhythm_model_runtime_missing", extra={"error_type": type(exc).__name__})
        return LoadedRhythmModel(
            state=RhythmModelState.RUNTIME_MISSING,
            source_path=path,
            detail="joblib is not installed; the model cannot be read",
        )

    try:
        artefact = joblib.load(path)
    except Exception as exc:
        # Broad on purpose. A pickle from a different scikit-learn version can raise almost
        # anything, and none of it should stop the API serving the paths that do not need it.
        # The exception *type* is logged; the message is not, because a pickle error can carry
        # file contents into a log line.
        log.warning(
            "rhythm_model_load_failed",
            extra={"model_path": str(path), "error_type": type(exc).__name__},
        )
        return LoadedRhythmModel(
            state=RhythmModelState.LOAD_FAILED,
            source_path=path,
            detail=(
                f"{type(exc).__name__} while reading the artefact. A pickle this size is bound "
                "to the scikit-learn version that wrote it; pin that version."
            ),
        )

    loaded = _read_bundle(artefact, settings, path)
    if loaded.is_ready:
        log.info(
            "rhythm_model_loaded",
            extra={
                "model_path": str(path),
                "model_version": loaded.version,
                "op_threshold": loaded.op_threshold,
                "op_threshold_is_fallback": loaded.op_threshold_is_fallback,
            },
        )
    return loaded


_lock = threading.Lock()
_cached: LoadedRhythmModel | None = None


def get_rhythm_model(settings: Settings | None = None) -> LoadedRhythmModel:
    """The loaded model, or the reason there is not one. Never raises.

    Usable as a FastAPI dependency directly. The double-checked lock matters because uvicorn
    serves from a thread pool and a 52 MB unpickle is not something to do twice.
    """
    global _cached
    if _cached is not None:
        return _cached
    with _lock:
        if _cached is None:
            _cached = _load(settings or get_settings())
    return _cached


def reset_rhythm_model() -> None:
    """Drop the cache. For tests, and for a deployment swapping the artefact across a restart."""
    global _cached
    with _lock:
        _cached = None
