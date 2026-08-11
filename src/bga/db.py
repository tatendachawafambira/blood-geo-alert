"""Engines, session factories, and the facility-scoped session wrapper.

Two engines, because two roles:
  app_engine    - bga_app. Every business read and write. Subject to RLS.
  audit_engine  - bga_audit. Insert-only on audit.audit_event, on its own
                  connection so that a rolled-back business transaction cannot
                  take the audit record with it (SAD 5.1).

ADR-017: scope is set with SET LOCAL inside the transaction, never SET.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from bga.config import settings


class Base(DeclarativeBase):
    """Declarative base shared by every model module.

    Alembic autogenerates against Base.metadata. Tables carry an explicit
    __table_args__ = {"schema": "core"} (or "audit"); nothing lives in public.
    """


app_engine = create_engine(settings.database_url_app, pool_pre_ping=True)
audit_engine = create_engine(settings.database_url_audit, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=app_engine, expire_on_commit=False)
AuditSessionLocal = sessionmaker(bind=audit_engine, expire_on_commit=False)


# set_config(name, value, is_local => true) IS "SET LOCAL". It is used instead
# of the SET LOCAL statement for one reason: SET LOCAL takes no bind parameter,
# so the only way to write it is to interpolate the value into SQL text.
# set_config takes a parameter, so the scope value is never formatted into SQL.
#
# The "true" third argument is the whole point. It scopes the setting to the
# current TRANSACTION. Connections are pooled and handed to the next request;
# a session-level SET would carry one facility's scope into another facility's
# request, which is the defect ADR-017 exists to prevent.
_SET_SCOPE = text(
    "SELECT set_config('app.facility_id', :facility_id, true),"
    "       set_config('app.actor_id',    :actor_id,    true),"
    "       set_config('app.role',        :role,        true)"
)


def _as_setting(value: UUID | str | None) -> str:
    # A GUC is text. NULL is not representable, so an absent scope is the empty
    # string. RLS policies must therefore read it as
    #   NULLIF(current_setting('app.facility_id', true), '')::uuid
    # because ''::uuid raises rather than returning NULL.
    return "" if value is None else str(value)


@contextmanager
def scoped_session(
    facility_id: UUID | str | None,
    actor_id: UUID | str | None = None,
    role: str | None = None,
) -> Iterator[Session]:
    """Open a transaction with the RLS scope set, and yield the session.

    Commits on clean exit, rolls back on exception. Every dashboard and domain
    query runs inside one of these; nothing queries app_engine directly.
    """
    with SessionLocal() as session:
        with session.begin():
            session.execute(
                _SET_SCOPE,
                {
                    "facility_id": _as_setting(facility_id),
                    "actor_id": _as_setting(actor_id),
                    "role": _as_setting(role),
                },
            )
            yield session
