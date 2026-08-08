"""Cross-process rate-limit counters.

The in-memory limiter counts per worker, so with N workers the effective ceiling is N times the
configured one. That is tolerable on the ingest endpoints, where the limit protects capacity. It
is not tolerable on the auth endpoints, where **the ceiling is the brute-force defence** — a
limit that silently multiplies by the worker count is not a defence, it is a number in a config
file.

Postgres rather than a second datastore, following the ``session_nonce`` precedent: correctness
here needs one shared counter, Postgres is already present, and Redis would be a new operational
dependency for one table.

Not a clinical table: it holds no clinical content, and counting requires updating rows, which
the append-only trigger would forbid.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UuidPkMixin


class RateLimitCounter(Base, UuidPkMixin):
    """One (bucket, subject, window) counter.

    The unique constraint is what makes the counter correct: the increment is a single
    ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING count``, so two workers cannot both read a
    count below the limit and both decide to allow the request.
    """

    __tablename__ = "rate_limit_counter"

    #: Which limit this counts against — ``auth_token_username``, ``auth_refresh_family``, and so
    #: on. Separate buckets so one endpoint's traffic cannot consume another's allowance.
    bucket: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    #: What is being counted: a username, a client address, a refresh-token family id.
    #:
    #: Hashed by the caller before it arrives here. An attempted username is a credential-adjacent
    #: value and a client address is personal data; neither belongs in a table that exists only to
    #: answer "how many".
    subject_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)

    #: Start of the fixed window, truncated to the window size.
    window_start: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    __table_args__ = (
        sa.UniqueConstraint(
            "bucket",
            "subject_key",
            "window_start",
            name="uq_rate_limit_counter_bucket_subject_window",
        ),
        # Expired windows are dead weight; this index is what makes purging them cheap.
        sa.Index("ix_rate_limit_counter_window_start", "window_start"),
    )
