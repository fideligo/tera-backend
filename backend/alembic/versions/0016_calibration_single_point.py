"""Let a calibration be derived from one session.

`ck_calibration_min_sessions` was `n_sessions >= 3`, from BUILD_SPEC 4.3's "at least three accepted
calibration sessions". The product has since committed to single-point calibration: the patient is
asked for one cuff reading, `min_calibration_sessions` is configuration set to 1, and
`pressure_estimate` fixes the intercept from one anchor while taking the slope from population
coefficients (invariant 1).

The constraint made that configured value unreachable, three layers below where anyone would look
for it — a single-session calibration was refused by the database whatever the setting said, and the
service raised first so nothing ever reported the real floor.

Invariant 10 says clinical thresholds are configuration with documented defaults, never hard-coded.
A CHECK pinning the policy figure breaks that rule in the one place a config change cannot reach.
What belongs at this level is the structural floor: a calibration derived from *no* sessions is not
a weak baseline, it is not a baseline. The policy figure stays in `config.py`.

Existing rows are unaffected — every calibration already stored has at least three sessions, so this
only widens what may be written next.
"""

from __future__ import annotations

from alembic import op

revision: str = "0016_calibration_single_point"
down_revision: str | None = "0015_realign_episode_beat_floor"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_calibration_min_sessions"


def upgrade() -> None:
    op.drop_constraint(CONSTRAINT, "calibration", type_="check")
    op.create_check_constraint(CONSTRAINT, "calibration", "n_sessions >= 1")


def downgrade() -> None:
    # Rows written under the relaxed rule would violate the old one. Removed first so the
    # downgrade fails loudly on data it cannot represent rather than silently leaving the
    # constraint off.
    op.drop_constraint(CONSTRAINT, "calibration", type_="check")
    op.create_check_constraint(CONSTRAINT, "calibration", "n_sessions >= 3")
