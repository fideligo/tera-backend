"""Bring existing episodes' min_beat_count back in step with the configured default.

`min_usable_beats` moved from 30 to 12 to match the ML reference's `MIN_PAIRS`, but
`protocol.min_beat_count` reads `monitoring_episode.protocol_params` first and only falls back to
settings. Every episode seeded before that change still carries a literal 30, so the config move had
no effect on them: a real capture with 17 usable beats was refused at ingest by a number that lived
in a JSONB column nobody had thought to look in.

Fixing the seed only helps episodes created afterwards. This corrects the rows that already exist,
which is the half a code change cannot reach — including the demo volume and anything already
deployed.

**Only the stale default is touched.** A row whose `min_beat_count` is 30 is the seed's old literal;
any other value was set deliberately for that episode and is left alone. `monitoring_episode` is
deliberately not in `APPEND_ONLY_TABLES` — protocol parameters are configuration a clinician may
tune, not a clinical measurement — so this is an ordinary update rather than a guard to work around.
"""

from __future__ import annotations

from alembic import op

revision: str = "0015_realign_episode_beat_floor"
down_revision: str | None = "0014_account_lifecycle_audit"
branch_labels = None
depends_on = None

#: The seed's old literal, and the only value this migration rewrites.
STALE_DEFAULT = 30

#: The configured default it should have been reading. Restated here rather than imported: a
#: migration describes the database at a point in time, and must not change meaning later when the
#: setting moves again.
NEW_DEFAULT = 12


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE monitoring_episode
        SET protocol_params = jsonb_set(
            protocol_params,
            '{{min_beat_count}}',
            '{NEW_DEFAULT}'::jsonb,
            false
        )
        WHERE protocol_params ->> 'min_beat_count' = '{STALE_DEFAULT}';
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE monitoring_episode
        SET protocol_params = jsonb_set(
            protocol_params,
            '{{min_beat_count}}',
            '{STALE_DEFAULT}'::jsonb,
            false
        )
        WHERE protocol_params ->> 'min_beat_count' = '{NEW_DEFAULT}';
        """
    )
