"""
ETF Flow Cache Service — Supabase-backed last-known-good store.

Multi-source scraper başarısız olduğunda bu cache'den son geçerli kaydı serve et.
Stale data tracking (24h+ vs 7d+) ile UI transparency sağla.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionLocal
from core.logger import get_logger
from models.etf_flow_cache import EtfFlowCache

logger = get_logger("etf_flow_cache_service")

CACHE_FRESH_MAX_AGE = timedelta(hours=25)
CACHE_STALE_MAX_AGE = timedelta(days=7)


async def save_etf_flow(
    symbol: str,
    net_flow_usd: float,
    net_flow_coins: float,
    source: str,
    spot_price: Optional[float] = None,
    total_aum_usd: Optional[float] = None,
    total_holdings_coins: Optional[float] = None,
    raw_data: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """
    Yeni bir ETF flow kaydını cache'e kaydet.
    Returns: id of inserted row or None on error.
    """
    try:
        async with AsyncSessionLocal() as session:
            entry = EtfFlowCache(
                symbol=symbol,
                net_flow_usd=net_flow_usd,
                net_flow_coins=net_flow_coins,
                spot_price=spot_price,
                total_aum_usd=total_aum_usd,
                total_holdings_coins=total_holdings_coins,
                source=source,
                raw_data=raw_data,
            )
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
            logger.info(
                f"✓ Cached {symbol} flow: ${net_flow_usd:,.0f} / "
                f"{net_flow_coins:,.2f} (source={source}, id={entry.id})"
            )
            return entry.id
    except Exception as e:
        logger.error(f"Cache save failed for {symbol}: {e}")
        return None


async def get_latest_etf_flow(symbol: str) -> Optional[Dict[str, Any]]:
    """
    En güncel cache kaydını al (son 7 gün içinde, en taze olanı).
    Returns: dict with flow data + freshness flags, or None.

    Output:
        {
            "net_flow_usd": ...,
            "net_flow_coins": ...,
            "spot_price": ...,
            "total_aum_usd": ...,
            "total_holdings_coins": ...,
            "source": "coinglass_scrape" | "btcetffundflow" | "bitbo" | "fmp_approx",
            "scraped_at": ISO 8601 string,
            "age_hours": float,
            "is_fresh": bool (≤25h),
            "is_stale": bool (>25h but ≤7d),
        }
    """
    try:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(EtfFlowCache)
                .where(EtfFlowCache.symbol == symbol)
                .order_by(desc(EtfFlowCache.scraped_at))
                .limit(1)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

        if not row:
            return None

        now = datetime.now(timezone.utc)
        scraped_at = row.scraped_at
        if scraped_at.tzinfo is None:
            scraped_at = scraped_at.replace(tzinfo=timezone.utc)

        age = now - scraped_at
        if age > CACHE_STALE_MAX_AGE:
            logger.warning(
                f"Cache for {symbol} too old ({age.days}d) — discarding"
            )
            return None

        return {
            "net_flow_usd": float(row.net_flow_usd),
            "net_flow_coins": float(row.net_flow_coins),
            "spot_price": float(row.spot_price) if row.spot_price else None,
            "total_aum_usd": float(row.total_aum_usd) if row.total_aum_usd else None,
            "total_holdings_coins": (
                float(row.total_holdings_coins)
                if row.total_holdings_coins
                else None
            ),
            "source": row.source,
            "scraped_at": scraped_at.isoformat(),
            "age_hours": round(age.total_seconds() / 3600, 1),
            "is_fresh": age <= CACHE_FRESH_MAX_AGE,
            "is_stale": age > CACHE_FRESH_MAX_AGE,
        }
    except Exception as e:
        logger.error(f"Cache read failed for {symbol}: {e}")
        return None


async def cleanup_old_cache_entries(keep_days: int = 30) -> int:
    """
    keep_days'den eski entry'leri sil. Background job'da çağrılabilir.
    Returns: deleted row count.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import delete
            stmt = delete(EtfFlowCache).where(EtfFlowCache.scraped_at < cutoff)
            result = await session.execute(stmt)
            await session.commit()
            count = result.rowcount or 0
            if count > 0:
                logger.info(f"Cleaned up {count} old etf_flow_cache entries")
            return count
    except Exception as e:
        logger.error(f"Cache cleanup failed: {e}")
        return 0
