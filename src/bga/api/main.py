"""The clinician-facing application. Port 8000.

Deliberately NOT the donor application. ADR-016 keeps the donor response page
on its own origin with a database capability limited to token redemption, so
that a compromise there yields the narrowest capability in the system. The two
are separate deployables and this file must never import the donor routes.

Block 7 adds the dashboard, gateway console and internal endpoints here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from bga.api import dashboard, receipts

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(
    title="Blood Geo Alert",
    description="Geo-fenced blood donor mobilisation - clinician application",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

app.include_router(receipts.router)
app.include_router(dashboard.router)
