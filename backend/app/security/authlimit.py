"""Cross-process rate limiting for the auth endpoints.

Separate from :mod:`app.security.ratelimit` on purpose. The in-memory limiter guards *capacity*
on the ingest endpoints, where an N-times-too-high ceiling costs some extra database work. This
one guards *credentials*, where the ceiling is the brute-force defence and a limit that silently
multiplies by the worker count is not a defence at all.

Counters live in Postgres (``rate_limit_counter``), incremented by a single
``INSERT ... ON CONFLICT DO UPDATE ... RETURNING count`` so two workers cannot both read a count
below the limit and both allow the request.

**Subject keys are hashed before they are stored.** An attempted username is credential-adjacent —
a failed-login table full of them is a list of usernames worth trying — and a client address is
personal data. Neither belongs in a table whose only job is to answer "how many". A keyed SHA-256
truncated to 32 hex characters keeps collisions negligible at this scale while making the stored
value useless on its own.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import RateLimitCounter
from app.security.ratelimit import RateLimitDecision


@dataclass(frozen=True)
class AuthLimit:
    """One configured limit: how many, over how long, in which bucket."""

    bucket: str
    limit: int
    window_seconds: int


def hash_subject(value: str) -> str:
    """Hash a rate-limit subject before it is stored.

    Not a password hash and not trying to be: the goal is that a dump of this table does not hand
    over a list of attempted usernames and client addresses, not that the values resist a targeted
    guess. The input space is small enough that a determined attacker with the table could confirm
    a specific guess, which is why nothing else depends on this being one-way.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def window_start_for(now: datetime, window_seconds: int) -> datetime:
    """Truncate ``now`` to the start of its fixed window."""
    epoch_seconds = int(now.timestamp())
    return datetime.fromtimestamp(
        epoch_seconds - (epoch_seconds % window_seconds), tz=UTC
    )


def check(
    db: Session,
    limit: AuthLimit,
    subject: str,
    *,
    now: datetime | None = None,
) -> RateLimitDecision:
    """Record one event and say whether it is allowed.

    The counter increments **whether or not the request is allowed**, unlike the in-memory
    limiter. Two reasons: it makes a caller that keeps hammering a locked bucket visible rather
    than invisible, and it is what lets the refresh endpoint tell "over the limit once" from
    "over the limit repeatedly", which is the signal that justifies revoking a token family.
    """
    now = now or datetime.now(UTC)
    window_start = window_start_for(now, limit.window_seconds)

    statement = (
        pg_insert(RateLimitCounter)
        .values(
            bucket=limit.bucket,
            subject_key=hash_subject(subject),
            window_start=window_start,
            count=1,
        )
        .on_conflict_do_update(
            constraint="uq_rate_limit_counter_bucket_subject_window",
            set_={"count": RateLimitCounter.__table__.c.count + 1},
        )
        .returning(RateLimitCounter.__table__.c.count)
    )
    count = db.execute(statement).scalar_one()
    # The counter is not clinical data and must survive the caller's transaction being rolled
    # back — a failed login rolls back, and a limiter that forgets failed attempts counts nothing.
    db.commit()

    window_end = window_start + timedelta(seconds=limit.window_seconds)
    retry_after = max(1, int((window_end - now).total_seconds()))

    return RateLimitDecision(
        allowed=count <= limit.limit,
        limit=limit.limit,
        remaining=max(0, limit.limit - count),
        retry_after_seconds=retry_after,
    )


def breach_depth(
    db: Session,
    limit: AuthLimit,
    subject: str,
    *,
    now: datetime | None = None,
) -> int:
    """How far past the limit this subject is in the current window, without incrementing.

    Used to decide whether a breach is a client bug worth tolerating or sustained abuse worth
    ending a login over.
    """
    now = now or datetime.now(UTC)
    window_start = window_start_for(now, limit.window_seconds)

    count = db.execute(
        sa.select(RateLimitCounter.count).where(
            RateLimitCounter.bucket == limit.bucket,
            RateLimitCounter.subject_key == hash_subject(subject),
            RateLimitCounter.window_start == window_start,
        )
    ).scalar_one_or_none()

    return max(0, (count or 0) - limit.limit)


def purge_expired(db: Session, *, older_than: datetime) -> int:
    """Delete counters whose window has passed. Returns how many went.

    Not a clinical table, so deletion is permitted here and nowhere near a patient record.
    """
    result = db.execute(
        sa.delete(RateLimitCounter).where(RateLimitCounter.window_start < older_than)
    )
    db.commit()
    return result.rowcount or 0
