"""Admin — Kurumsal Sentez manuel tetik (Commit 2).

BOT_INTERNAL_SECRET ile auth (core.security.assert_internal_secret).
Sentezi üretir + corporate_syntheses'e yazar. BROADCAST YOK (Commit 3) —
yalnız DB'ye yazar ve SynthResult döner.
"""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Header
from pydantic import BaseModel

from core.logger import get_logger
from core.security import assert_internal_secret
from services.corporate_synthesis import synthesize_week_safe

logger = get_logger("corporate_admin")

router = APIRouter(prefix="/admin/corporate", tags=["admin"])


class SynthesizeReq(BaseModel):
    week: Optional[date] = None          # None = bu hafta
    tier: Optional[str] = None           # None = premium+advance
    force: bool = False


@router.post("/synthesize")
async def trigger_synthesize(
    payload: SynthesizeReq,
    x_internal_secret: Optional[str] = Header(None),
):
    """Kurumsal haftalık sentezi NOW üret. Broadcast YOK; sadece DB'ye
    yazar. Tek tier istemek için tier='premium'|'advance'."""
    assert_internal_secret(x_internal_secret)

    if payload.tier in ("premium", "advance"):
        tiers = (payload.tier,)
    else:
        tiers = ("premium", "advance")

    results = await synthesize_week_safe(
        ref=payload.week, tiers=tiers, force=payload.force
    )
    return {
        "ok": True,
        "results": [
            {
                "event_id": r.event_id,
                "tier": r.tier,
                "week_start": r.week_start.isoformat() if r.week_start else None,
                "written": r.written,
                "skipped": r.skipped,
                "reason": r.reason,
                "word_count": r.word_count,
                "sources": r.sources,
            }
            for r in results
        ],
    }
