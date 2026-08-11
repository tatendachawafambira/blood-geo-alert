"""Alembic environment.

Runs as bga_owner (settings.database_url_owner) - migrations are DDL and the
runtime role deliberately holds no DDL grant.

Autogenerate is restricted to the core and audit schemas. PostGIS installs
spatial_ref_sys into public and creates the tiger/topology schemas; without
include_name those become "missing" tables and autogenerate emits DROP
statements for them.

alembic_version stays in the default schema. It must be writable before any
migration has run, and the first migration is what creates core and audit -
so "alembic upgrade head against an empty database" has to work with no
schema pre-existing.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from bga.config import settings
from bga.db import Base

# Importing the models package puts every table on Base.metadata. Without this
# import autogenerate produces an empty migration.
import bga.models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url_owner)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

MANAGED_SCHEMAS = {"core", "audit"}


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    if type_ == "schema":
        # None is the default schema (public). We manage nothing there.
        return name in MANAGED_SCHEMAS
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url_owner,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_schemas=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
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
            include_schemas=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
