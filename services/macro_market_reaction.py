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

FMP_API_KEY is shared with the existing fmp_service / etf_flow_scheduler
wiring (no new env). All errors are swallowed — if FMP is briefly down
or the price symbol changes, the reaction line just doesn't render.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("macro.market_reaction")

# FMP /stable/quote symbols. Index-style tickers vary in FMP support so we
# try the caret form first and fall back to the bare ticker / common
# alternatives — this keeps the line lit even if FMP rotates which form
# they accept.
_DXY_CANDIDATES = ("^DXY", "DXY", "DX-Y.NYB", "USDX")
_SPY_CANDIDATES = ("SPY",)
_US10Y_CANDIDATES = ("^TNX", "TNX", "US10Y")

_FMP_QUOTE_URL = "https://financialmodelingprep.com/stable/quote"
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Offsets we capture. Add T+30 / T+60 here later if useful.
_SNAPSHOT_OFFSETS = (0, 300)


async def _fetch_quote(client: httpx.AsyncClient, symbol: str, api_key: str) -> Optional[float]:
    try:
        r = await client.get(_FMP_QUOTE_URL, params={"symbol": symbol, "apikey": api_key})
        if r.status_code != 200:
            logger.debug(f"market reaction quote {symbol} HTTP {r.status_code}")
            return None
        body = r.json()
        if isinstance(body, list) and body:
            price = body[0].get("price")
            if price is not None and price != 0:
                return float(price)
    except Exception as e:
        logger.debug(f"market reaction quote {symbol} failed: {e}")
    return None


async def _fetch_quote_first_match(
    client: httpx.AsyncClient, candidates: tuple, api_key: str,
) -> Optional[float]:
    """Try each candidate symbol in order; return the first non-null price."""
    for sym in candidates:
        price = await _fetch_quote(client, sym, api_key)
        if price is not None:
            return price
    return None


async def _capture_snapshot(event_id: str, t_offset_seconds: int) -> bool:
    """Take one DXY/SPY/US10Y snapshot for the given event and persist."""
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        logger.debug("FMP_API_KEY missing — market reaction skipped")
        return False
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        dxy, spy, us10y = await asyncio.gather(
            _fetch_quote_first_match(client, _DXY_CANDIDATES, api_key),
            _fetch_quote_first_match(client, _SPY_CANDIDATES, api_key),
            _fetch_quote_first_match(client, _US10Y_CANDIDATES, api_key),
        )
    if dxy is None and spy is None and us10y is None:
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
    # T+5 with a sleep — keep this in the same task; if the worker dies
    # mid-sleep we just lose the second snapshot.
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
