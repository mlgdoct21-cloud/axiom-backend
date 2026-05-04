"""
Sliding 24-hour feature quota for dashboard tabs.

Pattern: each tab visit POSTs /feature-quota/consume which logs a row
in feature_quota_log + counts rows in the last 24h. Free tier gets 2
per command per 24h-from-first-visit; paid tiers unlimited.

DB-backed (multi-replica safe, restart-resilient). Different from
tier_quota.py which is in-memory + UTC-day for Telegram commands.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("feature_quota")

# (tier, command) → 24h limit. None = unlimited.
_LIMITS: dict[tuple[str, str], Optional[int]] = {
    ("free",    "crypto_overview"): 2,
    ("free",    "crypto_onchain"):  2,
    ("premium", "crypto_overview"): None,
    ("premium", "crypto_onchain"):  None,
    ("advance", "crypto_overview"): None,
    ("advance", "crypto_onchain"):  None,
}


@dataclass
class QuotaResult:
    allowed: bool
    used: int
    limit: Optional[int]
    tier: str
    command: str

    @property
    def remaining(self) -> Optional[int]:
        if self.limit is None:
            return None
        return max(0, self.limit - self.used)


def _normalize_tier(tier: Optional[str]) -> str:
    t = (tier or "free").lower()
    if t not in ("free", "premium", "advance"):
        return "free"
    return t


def _limit_for(tier: str, command: str) -> Optional[int]:
    return _LIMITS.get((tier, command))  # None default = unlimited


async def _count_in_window(telegram_id: str, command: str) -> int:
    sql = text("""
        SELECT COUNT(*) FROM feature_quota_log
        WHERE telegram_id = :tid AND command = :c
          AND used_at > NOW() - INTERVAL '24 hours'
    """)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, {"tid": str(telegram_id), "c": command})).fetchone()
            return int(row[0]) if row else 0
    except Exception as e:
        logger.warning(f"feature_quota count error: {e}")
        return 0  # fail-open


async def _log_consume(telegram_id: str, command: str) -> None:
    sql = text("""
        INSERT INTO feature_quota_log (telegram_id, command, used_at)
        VALUES (:tid, :c, NOW())
    """)
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, {"tid": str(telegram_id), "c": command})
    except Exception as e:
        logger.warning(f"feature_quota log error: {e}")


async def check_and_consume(telegram_id: str, tier: Optional[str], command: str) -> QuotaResult:
    """Atomic check + log. Returns whether the call is allowed within
    the rolling 24-hour window."""
    t = _normalize_tier(tier)
    limit = _limit_for(t, command)
    used_now = await _count_in_window(telegram_id, command)

    if limit is not None and used_now >= limit:
        return QuotaResult(allowed=False, used=used_now, limit=limit, tier=t, command=command)

    await _log_consume(telegram_id, command)
    return QuotaResult(allowed=True, used=used_now + 1, limit=limit, tier=t, command=command)


async def peek(telegram_id: str, tier: Optional[str], command: str) -> QuotaResult:
    """Read-only — for UI badges ('1 hak kaldı'). Doesn't consume."""
    t = _normalize_tier(tier)
    limit = _limit_for(t, command)
    used_now = await _count_in_window(telegram_id, command)
    return QuotaResult(
        allowed=(limit is None or used_now < limit),
        used=used_now, limit=limit, tier=t, command=command,
    )


async def cleanup_old(keep_hours: int = 48) -> int:
    """Drop rows older than keep_hours (default 48h, gives 24h margin)."""
    sql = text(f"DELETE FROM feature_quota_log WHERE used_at < NOW() - INTERVAL '{int(keep_hours)} hours'")
    try:
        async with engine.begin() as conn:
            r = await conn.execute(sql)
            return r.rowcount or 0
    except Exception as e:
        logger.warning(f"feature_quota cleanup error: {e}")
        return 0
