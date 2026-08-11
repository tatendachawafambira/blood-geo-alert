"""The donor response application. Port 8001, its own origin, its own DB role.

    uvicorn bga.api.donor_main:app --port 8001

ADR-016. This is the only component an unauthenticated stranger holding a link
can reach, so its capability is cut down until a compromise here is worth as
little as possible:

  Its database role, bga_donor, has no grant whatsoever on core.donor or
  core.outbox, and only (id, collection_point_id) on core.deficit_request. It
  cannot read a mobile number, a blood group, a facility or a ciphertext,
  because the DATABASE refuses - not because this code declines to ask.

  It imports nothing from matching, readmodel or the donor registry. There is
  no query path over the register to misuse.

  It is a separate ASGI application from bga.api.main, on a separate port. The
  50 KB transfer budget could not be met by a bundle shared with an
  authenticated dashboard.

KNOWN LIMITATION, worth saying out loud: this process still reads one .env, so
the application role's connection string is present in its environment even
though nothing here uses it. In deployment the donor container is given only
DATABASE_URL_DONOR. That is a packaging property, not one this file can
enforce.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from bga.api import donor

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
    title="Blood Geo Alert - donor response",
    description="Tokenised donor response page. No accounts, no passwords.",
    version="1.0.0",
    # No interactive docs on a public origin: they advertise the surface and
    # cost bytes on a budget measured in kilobytes.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(donor.router)
