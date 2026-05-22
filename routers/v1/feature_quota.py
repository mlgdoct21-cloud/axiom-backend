"""
POST /api/v1/feature-quota/consume
GET  /api/v1/feature-quota/peek

JWT-authenticated quota gate for dashboard features (Whitepaper +
On-Chain tabs). Frontend POSTs before rendering; if 402 → render
upgrade overlay instead of feature.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import engine, get_db
from core.security import get_current_user as get_authenticated_user
from services.feature_quota import check_and_consume, peek

router = APIRouter(prefix="/feature-quota", tags=["feature-quota"])

# Allowed feature commands — keep this list explicit so callers can't
# burn quota on arbitrary keys.
_ALLOWED = {"crypto_overview", "crypto_onchain", "news_read"}


class ConsumeRequest(BaseModel):
    command: Literal["crypto_overview", "crypto_onchain", "news_read"]


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


@router.get("/history")
async def quota_history(
    days: int = Query(7, ge=1, le=30),
    current_user = Depends(get_authenticated_user),
):
    """Recent feature_quota_log events for the authenticated user.

    Powers the Settings page "Kullanım Geçmişi" card. Capped at 30 days
    so the index scan stays cheap. Returns rows newest-first."""
    sql = text("""
        SELECT command, used_at FROM feature_quota_log
        WHERE telegram_id = :tid
          AND used_at > NOW() - make_interval(days => :days)
        ORDER BY used_at DESC
        LIMIT 200
    """)
    items: list[dict] = []
    try:
        async with engine.begin() as conn:
            rows = (await conn.execute(sql, {"tid": str(current_user.telegram_id), "days": days})).fetchall()
            for row in rows:
                ts = row[1]
                if ts is not None and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                items.append({
                    "command": row[0],
                    "used_at": ts.isoformat() if ts else None,
                })
    except Exception:
        items = []
    return {"days": days, "total": len(items), "items": items}


@router.get("/summary")
async def quota_summary(
    current_user = Depends(get_authenticated_user),
):
    """Single-shot summary for the dashboard header badge.

    Returns tier + per-command usage for every command in `_ALLOWED`,
    plus the earliest reset time across all commands (when the oldest
    used row expires from the 24h window). Premium/advance return
    `limit: null` and `remaining: null` to signal unlimited."""
    tier = (getattr(current_user, "tier", "free") or "free").lower()
    if tier not in ("free", "premium", "advance"):
        tier = "free"

    quotas: dict[str, dict] = {}
    for cmd in sorted(_ALLOWED):
        result = await peek(current_user.telegram_id, tier, cmd)
        quotas[cmd] = {
            "used": result.used,
            "limit": result.limit,
            "remaining": result.remaining,
            "allowed": result.allowed,
        }

    reset_at = await _earliest_reset(current_user.telegram_id)

    return {
        "tier": tier,
        "quotas": quotas,
        "reset_at": reset_at.isoformat() if reset_at else None,
    }


async def _earliest_reset(telegram_id: str) -> Optional[datetime]:
    """Oldest used_at within the 24h window + 24h = reset moment.
    None when there's nothing in the window."""
    sql = text("""
        SELECT MIN(used_at) FROM feature_quota_log
        WHERE telegram_id = :tid
          AND used_at > NOW() - INTERVAL '24 hours'
    """)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, {"tid": str(telegram_id)})).fetchone()
            if not row or row[0] is None:
                return None
            ts: datetime = row[0]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts + timedelta(hours=24)
    except Exception:
        return None


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
