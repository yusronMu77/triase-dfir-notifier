"""Endpoint internal notifier (`/internal/v1`). Setiap endpoint wajib token layanan."""

from astria_common.auth import require_service
from astria_common.db import Database
from astria_common.errors import AstriaError
from fastapi import APIRouter, Depends, Request

from astria_notifier.sender import outbox_counts

router = APIRouter(prefix="/internal/v1", tags=["notifier"])

_caller = require_service("svc:notifier", allowed_callers={"svc:api", "svc:config"})


@router.get("/status", dependencies=[Depends(_caller)])
async def status(request: Request) -> dict:
    """Jumlah baris `event_outbox` per status dan umur `pending` tertua (note app-notifier 3.3)."""
    db: Database | None = request.app.state.components.get("postgres")
    if db is None or not db.is_ready:
        raise AstriaError("NTF_DB_NOT_READY", "basis data notifier belum siap", 503)
    async with db.engine.connect() as conn:
        return await outbox_counts(conn)
