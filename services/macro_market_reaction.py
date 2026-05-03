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

Quote source: yfinance — Yahoo's index symbols (^DXY, ^TNX) are free and
intraday, while FMP /stable/quote rejects index tickers on free tier.
yfinance is already in requirements (used by daily_digest VIX + BIST).
"""
from __future__ import annotations

import asyncio
from typing import Optional

import yfinance as yf
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("macro.market_reaction")

# Yahoo Finance ticker symbols. ^DXY and ^TNX work on the free tier.
_DXY_SYMBOL = "DX-Y.NYB"  # primary
_DXY_FALLBACKS = ("^DXY",)
_SPY_SYMBOL = "SPY"
_US10Y_SYMBOL = "^TNX"  # CBOE 10-Year Treasury Yield Index, value in %

# Offsets we capture. Add T+30 / T+60 here later if useful.
_SNAPSHOT_OFFSETS = (0, 300)


def _yf_latest(symbol: str) -> Optional[float]:
    """Latest minute-bar close price for a Yahoo Finance ticker. Synchronous —
    callers wrap in asyncio.to_thread.
    """
    try:
        ticker = yf.Ticker(symbol)
        # 1-day intraday at 1-min resolution gives us the freshest tick.
        hist = ticker.history(period="1d", interval="1m")
        if hist.empty:
            # Markets closed / symbol stale — fall back to daily close.
            hist = ticker.history(period="5d", interval="1d")
            if hist.empty:
                return None
        price = float(hist["Close"].iloc[-1])
        if not price or price != price:  # NaN guard
            return None
        return price
    except Exception as e:
        logger.debug(f"yf {symbol} failed: {e}")
        return None


def _yf_first_match(candidates: tuple) -> Optional[float]:
    for sym in candidates:
        price = _yf_latest(sym)
        if price is not None:
            return price
    return None


async def _capture_snapshot(event_id: str, t_offset_seconds: int) -> bool:
    """Take one DXY/SPY/US10Y snapshot for the given event and persist."""
    dxy, spy, us10y = await asyncio.gather(
        asyncio.to_thread(_yf_first_match, (_DXY_SYMBOL,) + _DXY_FALLBACKS),
        asyncio.to_thread(_yf_latest, _SPY_SYMBOL),
        asyncio.to_thread(_yf_latest, _US10Y_SYMBOL),
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
