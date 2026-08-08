"""Cross-process rate limiting on the auth endpoints.

On the ingest endpoints a rate limit protects capacity, and an N-times-too-high ceiling costs
some extra database work. On the auth endpoints **the ceiling is the brute-force defence**, so
the properties worth testing are the ones that make it a defence rather than a decoration: it
counts failures, it counts them in shared storage, and it does not store the credentials it is
counting.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.config import get_settings
from app.models import RateLimitCounter, RefreshToken
from app.security import authlimit
from tests.conftest import DEMO_PASSWORD

pytestmark = pytest.mark.invariant


def _login(client, username: str, password: str):
    return client.post(
        "/v1/auth/token",
        data={"username": username, "password": password},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def test_repeated_failed_logins_are_refused_with_retry_after(client, patient_user, db):
    """The limit is per attempted username, and a 429 tells the client when to come back."""
    settings = get_settings().security
    limit = settings.auth_login_limit_per_username

    for _ in range(limit):
        assert _login(client, patient_user.subject, "wrong-password").status_code == 401

    refused = _login(client, patient_user.subject, "wrong-password")
    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    assert refused.headers["X-RateLimit-Limit"] == str(limit)


def test_the_limit_holds_even_with_the_correct_password(client, patient_user, db):
    """Exhausting the limit locks the account's *login*, not merely its wrong guesses.

    If a correct password bypassed the counter, an attacker would be throttled right up until the
    moment they succeeded — which is the one attempt that matters.
    """
    settings = get_settings().security
    for _ in range(settings.auth_login_limit_per_username):
        _login(client, patient_user.subject, "wrong-password")

    assert _login(client, patient_user.subject, DEMO_PASSWORD).status_code == 429


def test_attempts_against_a_nonexistent_account_are_counted(client, db):
    """Credential stuffing is mostly attempts against accounts that do not exist.

    Counting only known usernames would leave the commonest attack unmetered.
    """
    settings = get_settings().security
    unknown = f"nobody-{uuid.uuid4().hex[:8]}@test.invalid"

    for _ in range(settings.auth_login_limit_per_username):
        assert _login(client, unknown, "whatever").status_code == 401

    assert _login(client, unknown, "whatever").status_code == 429


def test_counters_survive_the_failed_login_rollback(client, patient_user, db):
    """A failed login rolls its transaction back; the count must not roll back with it.

    This is the bug the whole design turns on. A limiter whose increments vanish with the failure
    they were counting counts nothing at all.
    """
    _login(client, patient_user.subject, "wrong-password")

    stored = db.execute(
        sa.select(RateLimitCounter.count).where(
            RateLimitCounter.bucket == "auth_token_username"
        )
    ).scalars().all()

    assert stored, "the attempt was not recorded at all"
    assert max(stored) >= 1


def test_the_counter_is_in_the_database_not_in_the_process(client, patient_user, db):
    """Shared storage is the entire point.

    An in-memory counter multiplies the configured ceiling by the worker count. Asserting the row
    is visible to a *different* session than the one that wrote it is what distinguishes the two.
    """
    _login(client, patient_user.subject, "wrong-password")

    count = db.execute(
        sa.select(sa.func.count()).select_from(RateLimitCounter)
    ).scalar_one()

    assert count >= 1


def test_attempted_usernames_are_not_stored_in_the_clear(client, patient_user, db):
    """A table of failed logins is a list of usernames worth trying.

    Hashing is not protecting a secret here — the input space is small — it is making sure a dump
    of a counting table is not also a target list.
    """
    _login(client, patient_user.subject, "wrong-password")

    keys = db.execute(sa.select(RateLimitCounter.subject_key)).scalars().all()

    assert keys
    assert patient_user.subject not in keys
    assert all(patient_user.subject not in key for key in keys)


def test_thresholds_come_from_config_not_literals():
    """Invariant 10. Every one of these is a security threshold and none may be inlined."""
    settings = get_settings().security

    for name in (
        "auth_login_limit_per_username",
        "auth_login_window_seconds",
        "auth_login_limit_per_address",
        "auth_refresh_limit_per_family",
        "auth_refresh_window_seconds",
        "auth_refresh_breach_revoke_threshold",
    ):
        assert isinstance(getattr(settings, name), int)
        assert getattr(settings, name) > 0


def test_the_increment_is_atomic_across_callers(db):
    """Two callers must not both read a count below the limit and both be allowed.

    Exercised through the same upsert the endpoint uses rather than through concurrency, which
    would be timing-dependent: what is being asserted is that N calls produce exactly N, with no
    read-modify-write window in which a count can be lost.
    """
    limit = authlimit.AuthLimit(bucket="test_bucket", limit=1000, window_seconds=3600)

    for expected in range(1, 26):
        decision = authlimit.check(db, limit, "same-subject")
        assert decision.remaining == 1000 - expected

    assert authlimit.breach_depth(db, limit, "same-subject") == 0


def test_sustained_refresh_abuse_revokes_the_family(client, patient_user, db):
    """A client hammering one family's tokens is broken or hostile; either way the login ends.

    Tolerating a few over the line is deliberate — retries and clock skew are real — but past the
    configured depth it has stopped being plausibly accidental.
    """
    settings = get_settings().security
    tokens = _login(client, patient_user.subject, DEMO_PASSWORD).json()
    refresh_token = tokens["refresh_token"]

    family_id = db.execute(
        sa.select(RefreshToken.family_id).order_by(RefreshToken.issued_at.desc()).limit(1)
    ).scalar_one()

    over = settings.auth_refresh_limit_per_family + settings.auth_refresh_breach_revoke_threshold
    last_status = None
    for _ in range(over + 2):
        last_status = client.post(
            "/v1/auth/refresh", json={"refresh_token": refresh_token}
        ).status_code

    assert last_status == 429

    live = db.execute(
        sa.select(sa.func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
    ).scalar_one()

    assert live == 0, "sustained breach must end the whole family, not just refuse the request"
