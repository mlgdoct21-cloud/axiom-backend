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

# Stooq CSV (free, no auth) is reliable for FX/futures/ETFs; Yahoo's chart
# API is reliable for indices that stooq lacks (^TNX). We try the primary
# source for each symbol, fall back to the secondary, and persist whatever
# we got — partial rows are fine.
_STOOQ_URL = "https://stooq.com/q/l/"
_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_USER_AGENT = "Mozilla/5.0 (compatible; AxiomMacro/0.1)"
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Offsets we capture. Add T+30 / T+60 here later if useful.
_SNAPSHOT_OFFSETS = (0, 300)


async def _fetch_stooq(client: httpx.AsyncClient, ticker: str) -> Optional[float]:
    """Pull last-close price from stooq's CSV endpoint. Returns None for the
    'N/D' rows stooq emits when it has no data for a ticker."""
    try:
        r = await client.get(
            _STOOQ_URL,
            params={"s": ticker, "f": "sd2t2ohlcv", "h": "", "e": "csv"},
            headers={"User-Agent": _USER_AGENT},
        )
        if r.status_code != 200 or not r.text:
            return None
        lines = r.text.strip().splitlines()
        if len(lines) < 2:
            return None
        # CSV: Symbol,Date,Time,Open,High,Low,Close,Volume
        cols = lines[1].split(",")
        if len(cols) < 7 or cols[6] in ("N/D", ""):
            return None
        try:
            v = float(cols[6])
        except ValueError:
            return None
        return v if v and v == v else None
    except Exception as e:
        logger.debug(f"stooq {ticker} failed: {e}")
        return None


async def _fetch_yahoo_chart(client: httpx.AsyncClient, symbol: str) -> Optional[float]:
    """Pull `meta.regularMarketPrice` from Yahoo's chart API for one symbol."""
    try:
        r = await client.get(
            _YAHOO_CHART_URL.format(symbol=symbol),
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
        return v if v == v else None
    except Exception as e:
        logger.debug(f"yahoo chart {symbol} failed: {e}")
        return None


async def _fetch_dxy(client: httpx.AsyncClient) -> Optional[float]:
    """DXY: stooq DX.F (Dollar Index futures) → Yahoo DX-Y.NYB fallback."""
    v = await _fetch_stooq(client, "dx.f")
    if v is not None:
        return v
    return await _fetch_yahoo_chart(client, "DX-Y.NYB")


async def _fetch_spy(client: httpx.AsyncClient) -> Optional[float]:
    """SPY: stooq spy.us → Yahoo SPY fallback."""
    v = await _fetch_stooq(client, "spy.us")
    if v is not None:
        return v
    return await _fetch_yahoo_chart(client, "SPY")


async def _fetch_us10y(client: httpx.AsyncClient) -> Optional[float]:
    """10-yr yield: Yahoo ^TNX is the only reliable free source (stooq has
    no equivalent). Returns yield as % (4.25 means 4.25%)."""
    return await _fetch_yahoo_chart(client, "^TNX")


async def _capture_snapshot(event_id: str, t_offset_seconds: int) -> bool:
    """Take one DXY/SPY/US10Y snapshot for the given event and persist."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        dxy, spy, us10y = await asyncio.gather(
            _fetch_dxy(client),
            _fetch_spy(client),
            _fetch_us10y(client),
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
