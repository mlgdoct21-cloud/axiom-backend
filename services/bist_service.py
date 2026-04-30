"""
BIST (Borsa Istanbul) Data Service

Phase 1 (current): Uses yfinance with `.IS` suffix (ASELS.IS, GARAN.IS).
  - Free, ~15 minute delay (Yahoo Finance Turkey licensing).
  - Acceptable for analysis-focused dashboard.

Phase 2 (future): Swap internal fetch implementations to Twelve Data
  ($29/mo, real-time WebSocket). Public method signatures stay identical
  so callers (price_service, fundamentals) need zero changes.

Provider abstraction: BIST_PROVIDER env var ("yfinance" | "twelvedata").
"""

import asyncio
import os
from datetime import datetime
from typing import Dict, List, Optional

from core.logger import get_logger

logger = get_logger("bist_service")

BIST_PROVIDER = os.getenv("BIST_PROVIDER", "yfinance").lower()
BIST_CACHE_TTL = int(os.getenv("BIST_CACHE_TTL_SECONDS", "60"))

# In-memory cache: separate from US PriceService cache to avoid symbol collision
_price_cache: Dict[str, Dict] = {}
_price_cache_ts: Dict[str, datetime] = {}
_ohlcv_cache: Dict[str, List] = {}
_ohlcv_cache_ts: Dict[str, datetime] = {}


def _to_yf_symbol(symbol: str) -> str:
    """Convert BIST symbol to Yahoo Finance format (GARAN -> GARAN.IS)"""
    sym = symbol.upper().strip()
    if sym.endswith(".IS"):
        return sym
    return f"{sym}.IS"


def _is_cache_valid(ts_dict: Dict[str, datetime], key: str) -> bool:
    if key not in ts_dict:
        return False
    return (datetime.now() - ts_dict[key]).total_seconds() < BIST_CACHE_TTL


# ─────────────────────────────────────────────────────────────────
# yfinance backend (Phase 1)
# ─────────────────────────────────────────────────────────────────

def _yf_fetch_price_sync(symbol: str) -> Optional[Dict]:
    """Blocking yfinance call — must be wrapped in asyncio.to_thread"""
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed. Run: pip install yfinance")
        return None

    try:
        yf_symbol = _to_yf_symbol(symbol)
        ticker = yf.Ticker(yf_symbol)

        # fast_info is faster than .info for just price/change
        fast = ticker.fast_info
        last_price = fast.get("last_price") if hasattr(fast, "get") else getattr(fast, "last_price", None)
        prev_close = fast.get("previous_close") if hasattr(fast, "get") else getattr(fast, "previous_close", None)

        if last_price is None or prev_close is None:
            # Fallback to history
            hist = ticker.history(period="2d", interval="1d")
            if hist.empty or len(hist) < 1:
                logger.warning(f"No yfinance data for {yf_symbol}")
                return None
            last_price = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last_price

        change = float(last_price) - float(prev_close)
        change_pct = (change / float(prev_close) * 100) if prev_close else 0.0

        return {
            "price": round(float(last_price), 4),
            "change": round(change, 4),
            "change_percent": round(change_pct, 2),
            "previous_close": round(float(prev_close), 4),
            "timestamp": datetime.now().isoformat(),
            "source": "yfinance",
            "delayed_minutes": 15,
        }
    except Exception as e:
        logger.error(f"yfinance fetch failed for {symbol}: {e}")
        return None


def _yf_fetch_ohlcv_sync(symbol: str, period: str, limit: int) -> Optional[List]:
    """Blocking yfinance OHLCV call"""
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed")
        return None

    # Map our period strings to yfinance interval + period args
    interval_map = {
        "1m": ("1m", "7d"),
        "5m": ("5m", "60d"),
        "15m": ("15m", "60d"),
        "30m": ("30m", "60d"),
        "1h": ("60m", "730d"),
        "4h": ("60m", "730d"),  # yfinance has no 4h, fall back to 1h
        "1d": ("1d", "2y"),
        "1w": ("1wk", "5y"),
        "1mo": ("1mo", "10y"),
    }
    interval, yf_period = interval_map.get(period, ("1d", "2y"))

    try:
        yf_symbol = _to_yf_symbol(symbol)
        ticker = yf.Ticker(yf_symbol)
        hist = ticker.history(period=yf_period, interval=interval, auto_adjust=False)

        if hist.empty:
            logger.warning(f"yfinance returned empty history for {yf_symbol} ({period})")
            return None

        candles = []
        for ts, row in hist.tail(limit).iterrows():
            candles.append({
                "time": ts.isoformat(),
                "open": round(float(row["Open"]), 4),
                "high": round(float(row["High"]), 4),
                "low": round(float(row["Low"]), 4),
                "close": round(float(row["Close"]), 4),
                "volume": int(row["Volume"]) if row["Volume"] else 0,
            })
        return candles
    except Exception as e:
        logger.error(f"yfinance OHLCV failed for {symbol} ({period}): {e}")
        return None


def _yf_fetch_fundamentals_sync(symbol: str) -> Optional[Dict]:
    """Fetch company info + key metrics from yfinance"""
    try:
        import yfinance as yf
    except ImportError:
        return None

    try:
        yf_symbol = _to_yf_symbol(symbol)
        ticker = yf.Ticker(yf_symbol)
        info = ticker.info or {}

        if not info or len(info) < 5:
            logger.warning(f"yfinance returned empty info for {yf_symbol}")
            return None

        return {
            "symbol": symbol.upper(),
            "name": info.get("longName") or info.get("shortName") or symbol,
            "sector": info.get("sector", "N/A"),
            "industry": info.get("industry", "N/A"),
            "country": info.get("country", "Turkey"),
            "currency": info.get("currency", "TRY"),
            "exchange": "BIST",
            "market_cap": info.get("marketCap"),
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "eps": info.get("trailingEps"),
            "dividend_yield": info.get("dividendYield"),
            "book_value": info.get("bookValue"),
            "price_to_book": info.get("priceToBook"),
            "debt_to_equity": info.get("debtToEquity"),
            "return_on_equity": info.get("returnOnEquity"),
            "return_on_assets": info.get("returnOnAssets"),
            "profit_margin": info.get("profitMargins"),
            "operating_margin": info.get("operatingMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "earnings_growth": info.get("earningsGrowth"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow"),
            "average_volume": info.get("averageVolume"),
            "shares_outstanding": info.get("sharesOutstanding"),
            "description": info.get("longBusinessSummary", "")[:1000],
            "source": "yfinance",
        }
    except Exception as e:
        logger.error(f"yfinance fundamentals failed for {symbol}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────
# Twelve Data backend (Phase 2 placeholder)
# ─────────────────────────────────────────────────────────────────

async def _td_fetch_price(symbol: str) -> Optional[Dict]:
    """Twelve Data REST integration. Implement when migrating to Phase 2."""
    logger.warning("Twelve Data backend not implemented yet — falling back to yfinance")
    return await asyncio.to_thread(_yf_fetch_price_sync, symbol)


async def _td_fetch_ohlcv(symbol: str, period: str, limit: int) -> Optional[List]:
    logger.warning("Twelve Data backend not implemented yet — falling back to yfinance")
    return await asyncio.to_thread(_yf_fetch_ohlcv_sync, symbol, period, limit)


async def _td_fetch_fundamentals(symbol: str) -> Optional[Dict]:
    logger.warning("Twelve Data backend not implemented yet — falling back to yfinance")
    return await asyncio.to_thread(_yf_fetch_fundamentals_sync, symbol)


# ─────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────

class BISTService:
    """BIST data service — provider-agnostic public API"""

    @staticmethod
    async def fetch_latest_price(symbol: str) -> Optional[Dict]:
        symbol = symbol.upper()
        if _is_cache_valid(_price_cache_ts, symbol):
            cached = dict(_price_cache[symbol])
            cached["cached"] = True
            return cached

        if BIST_PROVIDER == "twelvedata":
            data = await _td_fetch_price(symbol)
        else:
            data = await asyncio.to_thread(_yf_fetch_price_sync, symbol)

        if data:
            _price_cache[symbol] = data
            _price_cache_ts[symbol] = datetime.now()
            data["cached"] = False
        return data

    @staticmethod
    async def fetch_ohlcv(symbol: str, period: str = "1d", limit: int = 100) -> Optional[List[Dict]]:
        symbol = symbol.upper()
        cache_key = f"{symbol}:{period}:{limit}"
        if _is_cache_valid(_ohlcv_cache_ts, cache_key):
            return _ohlcv_cache[cache_key]

        if BIST_PROVIDER == "twelvedata":
            candles = await _td_fetch_ohlcv(symbol, period, limit)
        else:
            candles = await asyncio.to_thread(_yf_fetch_ohlcv_sync, symbol, period, limit)

        if candles:
            _ohlcv_cache[cache_key] = candles
            _ohlcv_cache_ts[cache_key] = datetime.now()
        return candles

    @staticmethod
    async def fetch_fundamentals(symbol: str) -> Optional[Dict]:
        symbol = symbol.upper()
        if BIST_PROVIDER == "twelvedata":
            return await _td_fetch_fundamentals(symbol)
        return await asyncio.to_thread(_yf_fetch_fundamentals_sync, symbol)
