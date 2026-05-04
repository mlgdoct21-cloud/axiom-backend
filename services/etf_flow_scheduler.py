"""
ETF Flow Scheduler — Daily background scraper.

Multi-source fallback chain ile günde 1 kez BTC + ETH ETF flow data scrape eder
ve etf_flow_cache tablosuna kaydeder. Dashboard endpoint'i bu cache'den okur.

Schedule: startup'ta + her 24 saatte bir.
Error handling: scrape başarısız olursa 1 saat sonra retry.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from core.logger import get_logger
from services.etf_flow_scrapers import scrape_etf_flow_with_fallback
from services.etf_flow_cache_service import (
    save_etf_flow,
    get_latest_etf_flow,
    cleanup_old_cache_entries,
)

logger = get_logger("etf_flow_scheduler")

SCRAPE_INTERVAL = timedelta(hours=24)
RETRY_INTERVAL = timedelta(hours=1)
STARTUP_REFRESH_THRESHOLD = timedelta(hours=23)  # Startup'ta cache 23h+ eskiyse refresh


async def _fetch_spot_price(symbol: str) -> Optional[float]:
    """Coin spot price — CryptoQuant primary (Pro plan, kurumsal-kalite),
    FMP fallback (free tier, sometimes 402'd on indices).

    BTC için her iki kaynak da denenir; ETH için CryptoQuant'ta benzer
    endpoint var (/eth/market-data/price-ohlcv) ama Pro plan'da open
    olduğu doğrulanmadı, FMP'ye düşülür.
    """
    # 1. CryptoQuant (BTC only şimdilik)
    if symbol == "BTC":
        try:
            from services.cryptoquant_service import get_btc_spot_price
            cq_price = await get_btc_spot_price()
            if cq_price and cq_price > 0:
                return cq_price
        except Exception as e:
            logger.debug(f"CryptoQuant spot price unavailable, falling back: {e}")

    # 2. FMP fallback
    try:
        import os
        import aiohttp
        api_key = os.getenv("FMP_API_KEY", "").strip()
        if not api_key:
            return None
        sym = "BTCUSD" if symbol == "BTC" else "ETHUSD"
        url = f"https://financialmodelingprep.com/stable/quote?symbol={sym}&apikey={api_key}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        if isinstance(data, list) and data:
            return float(data[0].get("price") or 0) or None
    except Exception:
        pass
    return None


async def scrape_and_cache_one(symbol: str) -> bool:
    """Tek bir sembol için scrape + cache. Başarı durumu döner."""
    spot_price = await _fetch_spot_price(symbol)

    result = await scrape_etf_flow_with_fallback(symbol, spot_price=spot_price)
    if not result:
        logger.warning(f"Scrape failed for {symbol} — all sources down")
        return False

    await save_etf_flow(
        symbol=symbol,
        net_flow_usd=result["net_flow_usd"],
        net_flow_coins=result["net_flow_coins"],
        source=result["source"],
        spot_price=spot_price,
        total_aum_usd=result.get("total_aum_usd"),
        total_holdings_coins=result.get("total_holdings_coins"),
        raw_data=result,
    )
    return True


async def scrape_all_symbols() -> dict:
    """BTC + ETH paralel scrape + cache."""
    btc_ok, eth_ok = await asyncio.gather(
        scrape_and_cache_one("BTC"),
        scrape_and_cache_one("ETH"),
        return_exceptions=False,
    )
    return {"btc": btc_ok, "eth": eth_ok}


async def _needs_refresh(symbol: str) -> bool:
    """Cache 23h+ eskiyse veya yoksa True."""
    cached = await get_latest_etf_flow(symbol)
    if not cached:
        return True
    age = timedelta(hours=cached["age_hours"])
    return age >= STARTUP_REFRESH_THRESHOLD


async def etf_scraper_supervisor():
    """
    Background supervisor. Lifespan'de asyncio.create_task ile başlatılır.
    Startup'ta cache eski/yoksa hemen scrape eder, sonra 24h interval ile çalışır.
    """
    logger.info("ETF flow scraper supervisor started")

    # Startup check
    try:
        btc_needs = await _needs_refresh("BTC")
        eth_needs = await _needs_refresh("ETH")
        if btc_needs or eth_needs:
            logger.info(
                f"Startup: refreshing cache (btc_needs={btc_needs}, eth_needs={eth_needs})"
            )
            await scrape_all_symbols()
        else:
            logger.info("Startup: cache fresh, skipping initial scrape")
    except asyncio.CancelledError:
        logger.info("ETF scraper supervisor cancelled at startup")
        return
    except Exception as e:
        logger.error(f"Startup scrape error: {e}")

    while True:
        try:
            await asyncio.sleep(SCRAPE_INTERVAL.total_seconds())
            logger.info("Daily ETF flow scrape triggered")
            results = await scrape_all_symbols()
            if not (results["btc"] and results["eth"]):
                # En az biri başarısız → 1 saat sonra retry
                logger.warning(
                    f"Partial scrape failure ({results}); retry in 1h"
                )
                await asyncio.sleep(RETRY_INTERVAL.total_seconds())
                await scrape_all_symbols()

            # Haftalık cleanup
            now = datetime.now(timezone.utc)
            if now.hour < 4 and now.weekday() == 0:  # Pazartesi 00-04 UTC
                await cleanup_old_cache_entries(keep_days=30)

        except asyncio.CancelledError:
            logger.info("ETF scraper supervisor cancelled")
            break
        except Exception as e:
            logger.error(f"ETF scraper loop error: {e}; retry in 1h")
            try:
                await asyncio.sleep(RETRY_INTERVAL.total_seconds())
            except asyncio.CancelledError:
                break
