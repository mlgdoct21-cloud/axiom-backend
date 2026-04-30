"""Market Detection Service - Routes symbols to correct data source (US vs TR/BIST)"""

import os
from typing import Dict, List
from core.logger import get_logger

logger = get_logger("market_detector")


def _parse_symbols(env_var: str, default: str) -> List[str]:
    raw = os.getenv(env_var, default)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


BIST_SYMBOLS: List[str] = _parse_symbols(
    "BIST_SYMBOLS",
    "ASELS,GARAN,KCHOL,THYAO,AKBNK,BIMAS,EREGL,TAVHL,PETKM,SASA,SISE,TCELL,TUPRS,FROTO,YKBNK,HALKB,ISCTR,VAKBN,ARCLK,KOZAL"
)


class MarketDetector:
    """Detects whether a symbol belongs to BIST (TR) or US market"""

    @staticmethod
    def detect_market(symbol: str) -> str:
        """
        Returns "TR" for BIST symbols, "US" otherwise.

        BIST symbols are tracked in BIST_SYMBOLS env var.
        Comparison is case-insensitive.
        """
        if not symbol:
            return "US"
        return "TR" if symbol.upper() in BIST_SYMBOLS else "US"

    @staticmethod
    def is_bist(symbol: str) -> bool:
        return MarketDetector.detect_market(symbol) == "TR"

    @staticmethod
    def get_bist_symbols() -> List[str]:
        return list(BIST_SYMBOLS)

    @staticmethod
    def get_all_symbols() -> Dict[str, List[str]]:
        us_raw = os.getenv("TRADING_SYMBOLS", "BTC,ETH,AAPL,MSFT,GOOGL,TSLA")
        us_symbols = [s.strip().upper() for s in us_raw.split(",") if s.strip()]
        return {
            "US": us_symbols,
            "TR": list(BIST_SYMBOLS)
        }
