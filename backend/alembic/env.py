"""Alembic environment.

The database URL comes from ``TERA_DATABASE_URL`` (via ``app.config``) rather than alembic.ini,
so no credentials live in the repo.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings

# Importing the models package registers every table on Base.metadata.
from app.models import Base  # noqa: F401  (import for side effect + autogenerate target)

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silence every ``app.*`` logger
    # already created by module-level ``get_logger`` calls. Running a migration in the same
    # process as the API would then leave the application logging nothing at all.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
