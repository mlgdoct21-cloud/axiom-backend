"""Admin Macro router — reliability stats + manual probe trigger.

Hafta 1 verification kriteri (>=99% uptime, p95<3s) bu endpoint'ten
kontrol edilir. BOT_INTERNAL_SECRET ile auth.
"""
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from core.logger import get_logger
from services.macro_sources.reliability_probe import (
    probe_once,
    rolling_health_report,
)

logger = get_logger("macro_admin")

router = APIRouter(prefix="/admin/macro", tags=["admin"])


def _check_auth(x_internal_secret: Optional[str]) -> None:
    expected = os.getenv("BOT_INTERNAL_SECRET", "").strip()
    if not expected or x_internal_secret != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/health")
async def health_report(
    source: str = Query("fed_rss"),
    hours: int = Query(168, ge=1, le=720),
    x_internal_secret: Optional[str] = Header(None),
):
    _check_auth(x_internal_secret)
    return await rolling_health_report(source=source, hours=hours)


@router.post("/probe")
async def trigger_probe(x_internal_secret: Optional[str] = Header(None)):
    """Force-probe now (debug). Bypasses the 5-min interval."""
    _check_auth(x_internal_secret)
    return await probe_once()
