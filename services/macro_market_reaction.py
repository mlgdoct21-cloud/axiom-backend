"""Market reaction snapshots — capture DXY / SPY / US10Y shortly after a
macro release lands so the narrative can show "📉 Piyasa tepkisi (T+5dk):
DXY +0.4% | SPY -0.6% | US10Y +6bp" the way TradingEconomics does.

Triggered fire-and-forget from `macro_narrative.generate_narrative` once a
new narrative_md is persisted. We take two snapshots:
- T+0 :  immediately, captures the moment we detected the release
- T+5 :  300s later, captures the post-release reaction window

Deltas (%, %, bp) are computed at read time inside macro_public so the
DB stores raw quotes only. This keeps schema dumb and lets us tweak the
display formula without a backfill.

Quote source: Yahoo Finance v8 chart API directly via HTTP. yfinance lib
returns broken / wrong-field values for index tickers (saw `DX-Y.NYB`
come back as 25.7 instead of 98.2 in prod), and FMP's free tier rejects
indices. The chart API exposes the right value in `meta.regularMarketPrice`
without auth or rate-limit pain at our cadence (≤ a few releases/day).
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("macro.market_reaction")

# Yahoo Finance v8 chart endpoint — same URL the web client hits, no auth.
_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_USER_AGENT = "Mozilla/5.0 (compatible; AxiomMacro/0.1)"
_HTTP_TIMEOUT = httpx.Timeout(8.0, connect=5.0)

# Symbols that resolve to actual values via Yahoo:
# - DX-Y.NYB → ICE US Dollar Index spot (~98)
# - SPY      → SPY ETF (proxy for S&P 500)
# - ^TNX     → 10-Year Treasury Yield Index, value in % (4.25 = 4.25%)
_DXY_SYMBOL = "DX-Y.NYB"
_SPY_SYMBOL = "SPY"
_US10Y_SYMBOL = "^TNX"

# Offsets we capture. Add T+30 / T+60 here later if useful.
_SNAPSHOT_OFFSETS = (0, 300)


async def _fetch_quote(client: httpx.AsyncClient, symbol: str) -> Optional[float]:
    """Pull `meta.regularMarketPrice` from Yahoo's chart API for one symbol."""
    try:
        r = await client.get(
            _CHART_URL.format(symbol=symbol),
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": _USER_AGENT},
        )
        if r.status_code != 200:
            logger.debug(f"yahoo chart {symbol} HTTP {r.status_code}")
            return None
        body = r.json()
        results = body.get("chart", {}).get("result") or []
        if not results:
            return None
        meta = results[0].get("meta") or {}
        price = meta.get("regularMarketPrice")
        if price is None:
            return None
        try:
            v = float(price)
        except (TypeError, ValueError):
            return None
        # NaN guard
        return v if v == v else None
    except Exception as e:
        logger.debug(f"yahoo chart {symbol} failed: {e}")
        return None


async def _capture_snapshot(event_id: str, t_offset_seconds: int) -> bool:
    """Take one DXY/SPY/US10Y snapshot for the given event and persist."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        dxy, spy, us10y = await asyncio.gather(
            _fetch_quote(client, _DXY_SYMBOL),
            _fetch_quote(client, _SPY_SYMBOL),
            _fetch_quote(client, _US10Y_SYMBOL),
        )
    if dxy is None and spy is None and us10y is None:
        logger.warning(f"market reaction T+{t_offset_seconds} all None for {event_id}")
        return False
    sql = text("""
        INSERT INTO macro_release_market_snapshots
        (event_id, t_offset_seconds, dxy, spy, us10y)
        VALUES (:eid, :t, :dxy, :spy, :us10y)
    """)
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, {
                "eid": event_id, "t": t_offset_seconds,
                "dxy": dxy, "spy": spy, "us10y": us10y,
            })
        logger.info(
            f"market reaction T+{t_offset_seconds}s for {event_id}: "
            f"dxy={dxy} spy={spy} us10y={us10y}"
        )
        return True
    except Exception as e:
        logger.error(f"market reaction persist failed for {event_id} T+{t_offset_seconds}: {e}")
        return False


async def capture_reaction(event_id: str) -> None:
    """Fire-and-forget: T+0 immediately, T+5min after a sleep. Never raises.

    Idempotent against duplicate calls — the DB has no unique constraint
    here (snapshots are append-only) so a re-call would just stack rows.
    Callers (macro_narrative._trigger_broadcast peer) only fire once per
    new narrative_md write so this is safe in practice.
    """
    try:
        await _capture_snapshot(event_id, 0)
    except Exception as e:
        logger.error(f"capture_reaction T+0 crashed for {event_id}: {e}")
    try:
        await asyncio.sleep(_SNAPSHOT_OFFSETS[1])
        await _capture_snapshot(event_id, _SNAPSHOT_OFFSETS[1])
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"capture_reaction T+5 crashed for {event_id}: {e}")


def trigger_reaction(event_id: str) -> None:
    """Fire-and-forget wrapper used by macro_narrative — never raises and
    never blocks the caller."""
    try:
        asyncio.create_task(capture_reaction(event_id))
    except Exception as e:
        logger.error(f"trigger_reaction failed for {event_id}: {e}")
