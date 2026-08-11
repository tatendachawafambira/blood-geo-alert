"""The clinician-facing application. Port 8000.

Deliberately NOT the donor application. ADR-016 keeps the donor response page
on its own origin with a database capability limited to token redemption, so
that a compromise there yields the narrowest capability in the system. The two
are separate deployables and this file must never import the donor routes.

Block 7 adds the dashboard, gateway console and internal endpoints here.
"""

from __future__ import annotations

from fastapi import FastAPI

from bga.api import receipts

app = FastAPI(
    title="Blood Geo Alert",
    description="Geo-fenced blood donor mobilisation - clinician application",
    version="1.0.0",
)

app.include_router(receipts.router)
