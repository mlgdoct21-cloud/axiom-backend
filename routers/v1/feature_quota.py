"""
POST /api/v1/feature-quota/consume
GET  /api/v1/feature-quota/peek

JWT-authenticated quota gate for dashboard features (Whitepaper +
On-Chain tabs). Frontend POSTs before rendering; if 402 → render
upgrade overlay instead of feature.
"""
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.security import get_current_user as get_authenticated_user
from services.feature_quota import check_and_consume, peek

router = APIRouter(prefix="/feature-quota", tags=["feature-quota"])

# Allowed feature commands — keep this list explicit so callers can't
# burn quota on arbitrary keys.
_ALLOWED = {"crypto_overview", "crypto_onchain"}


class ConsumeRequest(BaseModel):
    command: Literal["crypto_overview", "crypto_onchain"]


@router.post("/consume")
async def consume(
    body: ConsumeRequest,
    current_user = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomic check + log. Returns 200 with quota status if allowed,
    402 (Payment Required) when limit hit so frontend can render
    upgrade overlay."""
    if body.command not in _ALLOWED:
        raise HTTPException(status_code=400, detail="Unknown command")

    tier = (getattr(current_user, "tier", "free") or "free").lower()
    result = await check_and_consume(current_user.telegram_id, tier, body.command)

    payload = {
        "allowed": result.allowed,
        "used": result.used,
        "limit": result.limit,
        "tier": result.tier,
        "command": result.command,
        "remaining": result.remaining,
    }
    if not result.allowed:
        return _paywall_response(payload)
    return payload


@router.get("/peek")
async def peek_quota(
    command: str = Query(...),
    current_user = Depends(get_authenticated_user),
):
    """Read-only — UI badges. Doesn't consume. Useful for showing
    '1 hak kaldı' before a click."""
    if command not in _ALLOWED:
        raise HTTPException(status_code=400, detail="Unknown command")
    tier = (getattr(current_user, "tier", "free") or "free").lower()
    result = await peek(current_user.telegram_id, tier, command)
    return {
        "allowed": result.allowed,
        "used": result.used,
        "limit": result.limit,
        "tier": result.tier,
        "command": result.command,
        "remaining": result.remaining,
    }


def _paywall_response(payload: dict):
    """402 with structured paywall info — frontend reads this to render
    the upgrade overlay."""
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "error": "quota_exceeded",
            "message": "Bugünkü ücretsiz hakkın doldu",
            **payload,
        },
    )
