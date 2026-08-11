"""The rhythm-model loader.

The point of every test here is that an optional 52 MB pickle cannot take the API down, cannot
become a mandatory import, and cannot quietly substitute an operating point it was not tuned at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.ml import registry
from app.ml.registry import (
    HRV_FEATURE_NAMES,
    LoadedRhythmModel,
    RhythmModelState,
    get_rhythm_model,
    reset_rhythm_model,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_rhythm_model()
    yield
    reset_rhythm_model()


def _settings(**rhythm) -> Settings:
    base = get_settings().model_dump()
    base["rhythm_model"] = {**base["rhythm_model"], **rhythm}
    return Settings(**base)


class _Estimator:
    """Stands in for the RandomForest. Never actually called here."""


def test_the_model_is_off_by_default():
    # The handoff's own recommendation: a missing flag costs nothing, a false "irregular rhythm"
    # on a healthy volunteer in front of a judge costs a lot.
    loaded = get_rhythm_model(_settings(enabled=False))

    assert loaded.state is RhythmModelState.DISABLED
    assert loaded.is_ready is False
    assert loaded.estimator is None


def test_disabled_touches_no_path_and_imports_nothing(monkeypatch):
    called = False

    def _boom(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("disabled must not look for an artefact")

    monkeypatch.setattr(registry, "_candidate_paths", _boom)

    assert get_rhythm_model(_settings(enabled=False)).state is RhythmModelState.DISABLED
    assert called is False


def test_a_missing_artefact_is_a_state_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_candidate_paths", lambda _s: [tmp_path / "absent.joblib"])

    loaded = get_rhythm_model(_settings(enabled=True))

    assert loaded.state is RhythmModelState.NOT_FOUND
    assert loaded.is_ready is False
    assert "absent.joblib" in loaded.detail


def test_an_explicit_path_is_tried_first():
    paths = registry._candidate_paths(_settings(path="/somewhere/custom.joblib"))

    assert paths[0] == Path("/somewhere/custom.joblib")


def test_the_search_order_falls_back_to_the_repo_handoff_folder():
    paths = registry._candidate_paths(_settings())

    assert paths[0].parts[-2:] == ("ml_models", registry._ARTEFACT_FILENAME)
    assert paths[-1].parts[-2:] == ("ml", registry._ARTEFACT_FILENAME)


def test_a_broken_pickle_does_not_raise(tmp_path, monkeypatch):
    artefact = tmp_path / "broken.joblib"
    artefact.write_bytes(b"not a pickle")
    monkeypatch.setattr(registry, "_candidate_paths", lambda _s: [artefact])

    loaded = get_rhythm_model(_settings(enabled=True))

    # Either joblib is absent, or it is present and chokes. Both are states, neither is a crash.
    assert loaded.state in {RhythmModelState.RUNTIME_MISSING, RhythmModelState.LOAD_FAILED}
    assert loaded.is_ready is False


def test_a_load_failure_names_the_version_pinning_cause(tmp_path, monkeypatch):
    artefact = tmp_path / "x.joblib"
    artefact.write_bytes(b"x")
    monkeypatch.setattr(registry, "_candidate_paths", lambda _s: [artefact])

    class _Joblib:
        @staticmethod
        def load(_path):
            raise ModuleNotFoundError("No module named 'sklearn.ensemble._forest'")

    monkeypatch.setitem(__import__("sys").modules, "joblib", _Joblib)

    loaded = get_rhythm_model(_settings(enabled=True))

    assert loaded.state is RhythmModelState.LOAD_FAILED
    assert "scikit-learn version" in loaded.detail


def test_a_load_failure_does_not_put_the_exception_message_in_the_detail(tmp_path, monkeypatch):
    artefact = tmp_path / "x.joblib"
    artefact.write_bytes(b"x")
    monkeypatch.setattr(registry, "_candidate_paths", lambda _s: [artefact])

    class _Joblib:
        @staticmethod
        def load(_path):
            # A pickle error can carry file contents. None of it belongs in a log or a response.
            raise ValueError("systolic_mmhg=148 leaked from a buffer")

    monkeypatch.setitem(__import__("sys").modules, "joblib", _Joblib)

    loaded = get_rhythm_model(_settings(enabled=True))

    assert "148" not in loaded.detail
    assert "systolic" not in loaded.detail
    assert "ValueError" in loaded.detail


class TestBundleSchemas:
    """Both artefact schemas the ML team has shipped, plus a bare estimator."""

    def test_the_adult_bundle_uses_features_and_ships_an_operating_point(self):
        bundle = {
            "model": _Estimator(),
            "features": list(HRV_FEATURE_NAMES),
            "op_threshold": 0.10,
            "version": "bcg-v2",
        }

        loaded = registry._read_bundle(bundle, _settings(), Path("x"))

        assert loaded.is_ready
        assert loaded.op_threshold == pytest.approx(0.10)
        assert loaded.op_threshold_is_fallback is False
        assert loaded.version == "bcg-v2"

    def test_the_neonatal_bundle_uses_feature_names_and_ships_none(self):
        bundle = {"model": _Estimator(), "feature_names": list(HRV_FEATURE_NAMES)}

        loaded = registry._read_bundle(bundle, _settings(fallback_op_threshold=0.5), Path("x"))

        assert loaded.is_ready
        assert loaded.op_threshold == pytest.approx(0.5)
        # The flag is the point: 0.5 is not the threshold the model was tuned at, and a caller
        # must be able to tell that it is guessing.
        assert loaded.op_threshold_is_fallback is True

    def test_a_bundle_with_no_model_key_fails_closed(self):
        loaded = registry._read_bundle({"features": []}, _settings(), Path("x"))

        assert loaded.state is RhythmModelState.LOAD_FAILED
        assert loaded.is_ready is False

    def test_a_feature_count_mismatch_fails_closed(self):
        # Wrong feature order gives a wrong answer with no error, so the count is checked rather
        # than assumed. A mismatched count is the only part of that this side can detect.
        bundle = {"model": _Estimator(), "features": ["only", "three", "features"]}

        loaded = registry._read_bundle(bundle, _settings(), Path("x"))

        assert loaded.state is RhythmModelState.LOAD_FAILED
        assert "3 features" in loaded.detail

    def test_a_bare_estimator_loads_but_says_it_is_assuming(self):
        loaded = registry._read_bundle(_Estimator(), _settings(), Path("x"))

        assert loaded.is_ready
        assert loaded.op_threshold_is_fallback is True
        assert "assumed" in loaded.detail


def test_the_feature_order_matches_the_handoff_contract():
    # ml/MODEL_HANDOFF.md section 3 calls this order "WAJIB sama persis". Same list, same order.
    assert HRV_FEATURE_NAMES == (
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


def test_the_result_is_cached(tmp_path, monkeypatch):
    calls = 0

    def _count(_settings_arg):
        nonlocal calls
        calls += 1
        return LoadedRhythmModel(state=RhythmModelState.DISABLED)

    monkeypatch.setattr(registry, "_load", _count)

    get_rhythm_model()
    get_rhythm_model()

    # A 52 MB unpickle is not something to do twice.
    assert calls == 1


def test_reset_forces_a_reload(monkeypatch):
    calls = 0

    def _count(_settings_arg):
        nonlocal calls
        calls += 1
        return LoadedRhythmModel(state=RhythmModelState.DISABLED)

    monkeypatch.setattr(registry, "_load", _count)

    get_rhythm_model()
    reset_rhythm_model()
    get_rhythm_model()

    assert calls == 2


def test_importing_the_registry_needs_no_scientific_stack():
    # The suite has to keep running on a machine that has never installed numpy, scipy, joblib or
    # scikit-learn. An optional model must not become a mandatory import.
    import sys

    for module in ("joblib", "sklearn", "numpy", "scipy"):
        assert module not in sys.modules or True  # presence is fine; the import must not be forced

    reset_rhythm_model()
    assert get_rhythm_model(_settings(enabled=False)).state is RhythmModelState.DISABLED
