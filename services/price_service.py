"""Price data fetching service - Alpha Vantage (US) + yfinance/BIST routing"""

import aiohttp
import asyncio
import logging
import os
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from core.logger import get_logger
from services.market_detector import MarketDetector
from services.bist_service import BISTService

logger = get_logger("price_service")

ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "demo")
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"
PRICE_CACHE_TTL = int(os.getenv("PRICE_CACHE_TTL_SECONDS", "60"))

# In-memory cache
price_cache: Dict[str, Dict] = {}
cache_timestamps: Dict[str, datetime] = {}


class OHLCV:
    """OHLCV data structure"""
    def __init__(self, time: str, open: float, high: float, low: float, close: float, volume: int):
        self.time = time
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume

    def to_dict(self) -> Dict:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume
        }


class PriceService:
    """Service for fetching and caching price data"""

    @staticmethod
    def is_cache_valid(symbol: str) -> bool:
        """Check if cache is still valid"""
        if symbol not in cache_timestamps:
            return False

        age = (datetime.now() - cache_timestamps[symbol]).total_seconds()
        return age < PRICE_CACHE_TTL

    @staticmethod
    async def fetch_latest_price(symbol: str) -> Optional[Dict]:
        """
        Fetch latest price for a symbol
        Routes BIST symbols (ASELS, GARAN, ...) to BISTService (yfinance).
        US symbols continue through Alpha Vantage.
        Returns: {"price": float, "change": float, "timestamp": str}
        """
        # Route BIST symbols to dedicated service
        if MarketDetector.is_bist(symbol):
            return await BISTService.fetch_latest_price(symbol)

        try:
            # Check cache
            if PriceService.is_cache_valid(symbol) and symbol in price_cache:
                cached = price_cache[symbol]
                return {
                    "price": cached.get("close"),
                    "change": cached.get("change", 0),
                    "timestamp": cached.get("timestamp"),
                    "cached": True
                }

            # Fetch from API
            params = {
                "function": "GLOBAL_QUOTE",
                "symbol": symbol,
                "apikey": ALPHA_VANTAGE_API_KEY,
                "outputsize": "compact"
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(ALPHA_VANTAGE_BASE_URL, params=params) as response:
                    data = await response.json()

            # Parse response
            if "Global Quote" not in data or not data["Global Quote"]:
                logger.warning(f"No data for symbol {symbol}")
                return None

            quote = data["Global Quote"]
            price = float(quote.get("05. price", 0))
            change = float(quote.get("09. change", 0))
            timestamp = quote.get("07. latest trading day", datetime.now().isoformat())

            # Cache result
            price_cache[symbol] = {
                "close": price,
                "change": change,
                "timestamp": timestamp
            }
            cache_timestamps[symbol] = datetime.now()

            return {
                "price": price,
                "change": change,
                "timestamp": timestamp,
                "cached": False
            }

        except Exception as e:
            logger.error(f"Error fetching price for {symbol}: {e}")
            return None

    @staticmethod
    async def fetch_ohlcv(symbol: str, period: str = "1h", limit: int = 100) -> Optional[List[OHLCV]]:
        """
        Fetch OHLCV data for a symbol
        period: "1m", "5m", "15m", "30m", "1h", "4h", "1d"
        Returns: List of OHLCV objects
        """
        # Route BIST symbols to BISTService and convert dict candles to OHLCV objects
        if MarketDetector.is_bist(symbol):
            candles = await BISTService.fetch_ohlcv(symbol, period, limit)
            if not candles:
                return None
            return [
                OHLCV(
                    time=c["time"],
                    open=c["open"],
                    high=c["high"],
                    low=c["low"],
                    close=c["close"],
                    volume=c["volume"],
                )
                for c in candles
            ]

        try:
            # Map periods to Alpha Vantage functions
            function_map = {
                "1m": "TIME_SERIES_INTRADAY",
                "5m": "TIME_SERIES_INTRADAY",
                "15m": "TIME_SERIES_INTRADAY",
                "30m": "TIME_SERIES_INTRADAY",
                "1h": "TIME_SERIES_INTRADAY",
                "4h": "TIME_SERIES_INTRADAY",
                "1d": "TIME_SERIES_DAILY",
                "1w": "TIME_SERIES_WEEKLY",
                "1mo": "TIME_SERIES_MONTHLY"
            }

            function = function_map.get(period, "TIME_SERIES_DAILY")

            params = {
                "function": function,
                "symbol": symbol,
                "apikey": ALPHA_VANTAGE_API_KEY,
                "outputsize": "compact"
            }

            # Add interval for intraday data
            if function == "TIME_SERIES_INTRADAY":
                interval_map = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "60min"}
                params["interval"] = interval_map.get(period, "60min")

            logger.info(f"Fetching OHLCV for {symbol} ({period})")

            async with aiohttp.ClientSession() as session:
                async with session.get(ALPHA_VANTAGE_BASE_URL, params=params) as response:
                    data = await response.json()

            # Parse response
            time_series_key = None
            if function == "TIME_SERIES_INTRADAY":
                # Find the correct key (varies based on interval)
                for key in data.keys():
                    if key.startswith("Time Series"):
                        time_series_key = key
                        break
            elif function == "TIME_SERIES_DAILY":
                time_series_key = "Time Series (Daily)"
            elif function == "TIME_SERIES_WEEKLY":
                time_series_key = "Weekly Time Series"
            elif function == "TIME_SERIES_MONTHLY":
                time_series_key = "Monthly Time Series"

            if not time_series_key or time_series_key not in data:
                logger.warning(f"No time series data for {symbol}")
                return None

            time_series = data[time_series_key]
            ohlcv_list: List[OHLCV] = []

            for timestamp, values in sorted(time_series.items(), reverse=True)[:limit]:
                try:
                    ohlcv = OHLCV(
                        time=timestamp,
                        open=float(values.get("1. open", 0)),
                        high=float(values.get("2. high", 0)),
                        low=float(values.get("3. low", 0)),
                        close=float(values.get("4. close", 0)),
                        volume=int(values.get("5. volume", 0))
                    )
                    ohlcv_list.append(ohlcv)
                except (ValueError, KeyError) as e:
                    logger.warning(f"Error parsing OHLCV for {timestamp}: {e}")
                    continue

            return list(reversed(ohlcv_list))  # Return in chronological order

        except Exception as e:
            logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return None

    @staticmethod
    def get_supported_symbols() -> List[str]:
        """Get list of supported symbols (US + BIST combined)"""
        us_str = os.getenv("TRADING_SYMBOLS", "BTC,ETH,AAPL,MSFT,GOOGL,TSLA")
        us_symbols = [s.strip().upper() for s in us_str.split(",") if s.strip()]
        bist_symbols = MarketDetector.get_bist_symbols()
        # Preserve order: US first, then TR
        seen = set()
        combined = []
        for s in us_symbols + bist_symbols:
            if s not in seen:
                combined.append(s)
                seen.add(s)
        return combined

    @staticmethod
    async def validate_symbol(symbol: str) -> bool:
        """Validate if symbol is supported (US or BIST)"""
        supported = PriceService.get_supported_symbols()
        return symbol.upper() in supported
