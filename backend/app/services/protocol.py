"""Per-episode protocol parameters.

Invariant 10: clinical thresholds are configuration. ``monitoring_episode.protocol_params`` is
the per-episode layer over the application defaults in ``app.config`` — a clinic can widen the
deviation multiplier for one patient without editing code or redeploying.

Reading a threshold anywhere else in ``app/`` means reading it from here or from ``app.config``.
Never a literal.
"""

from __future__ import annotations

from app.config import Settings
from app.models import MonitoringEpisode

#: Keys recognised in ``protocol_params``. Anything else is ignored rather than rejected, so a
#: clinic can annotate an episode without the API dictating its whole vocabulary.
DEVIATION_K = "deviation_k"
MIN_BEAT_COUNT = "min_beat_count"
PERSISTENCE_WINDOW_HOURS = "persistence_window_hours"
CUFF_SCHEDULE = "cuff_schedule"


def deviation_k(episode: MonitoringEpisode, settings: Settings) -> float:
    """The deviation multiplier k (BUILD_SPEC 4.3, default 2, configurable per episode)."""
    value = episode.protocol_params.get(DEVIATION_K)
    return float(value) if _is_positive_number(value) else settings.deviation.deviation_k


def min_beat_count(episode: MonitoringEpisode, settings: Settings) -> int:
    """Minimum usable beats for a completed session."""
    value = episode.protocol_params.get(MIN_BEAT_COUNT)
    return int(value) if _is_positive_number(value) else settings.deviation.min_usable_beats


def persistence_window_hours(episode: MonitoringEpisode, settings: Settings) -> int:
    """How long a deviation stays 'recent enough' for a repeat to count as persistent.

    DEVIATION from BUILD_SPEC 4.1, which lists only cuff schedule, k and minimum beat count in
    ``protocol_params``. BUILD_SPEC 4.3 requires "a repeat session within the configured window"
    but never names the setting, so it is added here rather than hard-coded.
    """
    value = episode.protocol_params.get(PERSISTENCE_WINDOW_HOURS)
    return (
        int(value) if _is_positive_number(value) else settings.deviation.persistence_window_hours
    )


def cuff_schedule(episode: MonitoringEpisode) -> dict:
    """The episode's cuff schedule, or an empty dict if none was set."""
    value = episode.protocol_params.get(CUFF_SCHEDULE)
    return value if isinstance(value, dict) else {}


def _is_positive_number(value: object) -> bool:
    """A protocol override only counts if it is a usable positive number.

    A malformed override falls back to the documented default rather than raising: a typo in one
    episode's JSON must not take the ingest path down for that patient.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
