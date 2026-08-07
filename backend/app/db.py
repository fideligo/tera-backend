"""Database engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,  # the demo runs against a container that may have been restarted
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Return the process-wide session factory."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False, future=True)


def reset_engine_cache() -> None:
    """Dispose and clear the cached engine. Used by tests and CLIs that re-point the URL."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    The session is committed by the route on success; on any exception it is rolled back, so a
    partially applied ingest can never be persisted.
    """
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for CLI use."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
