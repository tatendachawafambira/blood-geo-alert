"""Shared test fixtures.

The test database is dropped and rebuilt from migrations at the start of every
run. That is a second or two per session, and it buys two things: tests never
inherit yesterday's rows, and "alembic upgrade head against an empty database
produces the full schema" - acceptance requirement 1 - is re-proven every time
anyone runs pytest, rather than once by hand.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from bga.config import settings

TEST_DB_NAME = "bga_test"


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


@pytest.fixture(scope="session")
def test_db_url() -> str:
    """Drop and recreate bga_test, then migrate it to head."""
    owner_url = settings.database_url_owner
    admin_url = _with_database(owner_url, "postgres")
    target_url = _with_database(owner_url, TEST_DB_NAME)

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        # WITH (FORCE) terminates any leftover connection from a previous run,
        # which is otherwise the usual reason a drop fails on a dev machine.
        conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {TEST_DB_NAME} OWNER bga_owner"))
    admin.dispose()

    # alembic/env.py reads settings.database_url_owner, so point it at the test
    # database for the duration of the migration.
    original = settings.database_url_owner
    settings.database_url_owner = target_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        settings.database_url_owner = original

    return target_url


@pytest.fixture(scope="session")
def test_engine(test_db_url: str):
    engine = create_engine(test_db_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(test_engine) -> Session:
    """A session whose transaction is rolled back after the test.

    Nothing a test writes survives it, so tests may run in any order.
    """
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(scope="session")
def session_factory(test_engine) -> sessionmaker:
    """For tests that need two genuinely concurrent connections (Block 3)."""
    return sessionmaker(bind=test_engine)
