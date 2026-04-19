"""Trading Signals Service - Generate BUY/SELL/HOLD signals"""

from typing import Dict, Optional, List
from enum import Enum
from services.price_service import PriceService
from services.indicator_service import IndicatorService
from core.logger import get_logger

logger = get_logger("signal_service")


class SignalType(Enum):
    """Signal types"""
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class SignalService:
    """Service for generating trading signals"""

    @staticmethod
    async def generate_signal(symbol: str) -> Optional[Dict]:
        """
        Generate trading signal for a symbol
        Returns: {
            "symbol": str,
            "signal": "BUY|SELL|HOLD",
            "confidence": float (0-1),
            "reason": str,
            "indicators": {
                "rsi": float,
                "macd": Dict,
                "bollinger_bands": Dict
            },
            "price": float,
            "timestamp": str
        }
        """
        try:
            logger.info(f"Generating signal for {symbol}")

            # Fetch price data (1 day candles for good signal)
            ohlcv_list = await PriceService.fetch_ohlcv(symbol, period="1d", limit=50)
            if not ohlcv_list:
                logger.warning(f"No OHLCV data for {symbol}")
                return None

            # Extract closing prices
            prices = [ohlcv.close for ohlcv in ohlcv_list]

            # Calculate indicators
            indicators = IndicatorService.calculate_all_indicators(prices)
            if not indicators:
                logger.warning(f"Could not calculate indicators for {symbol}")
                return None

            # Generate signal logic
            signal, confidence, reason = SignalService._evaluate_signal(indicators)

            return {
                "symbol": symbol,
                "signal": signal.value,
                "confidence": confidence,
                "reason": reason,
                "indicators": {
                    "rsi": indicators.get("rsi"),
                    "macd": indicators.get("macd", {}).get("last_value") if indicators.get("macd") else None,
                    "bollinger_bands": indicators.get("bollinger_bands"),
                    "sma_20": indicators.get("sma_20"),
                    "sma_50": indicators.get("sma_50")
                },
                "price": indicators.get("current_price"),
                "timestamp": ohlcv_list[-1].time if ohlcv_list else None
            }

        except Exception as e:
            logger.error(f"Error generating signal for {symbol}: {e}")
            return None

    @staticmethod
    def _evaluate_signal(indicators: Dict) -> tuple:
        """
        Evaluate signal based on indicators
        Returns: (signal_type, confidence, reason)
        """
        signal = SignalType.HOLD
        confidence = 0.5
        reasons = []

        rsi = indicators.get("rsi")
        macd = indicators.get("macd", {}).get("last_value")
        bb = indicators.get("bollinger_bands")
        sma_20 = indicators.get("sma_20")
        sma_50 = indicators.get("sma_50")
        current_price = indicators.get("current_price")

        buy_signals = 0
        sell_signals = 0

        # RSI Analysis
        if rsi:
            if rsi < 30:
                buy_signals += 2
                reasons.append("RSI oversold (<30)")
            elif rsi > 70:
                sell_signals += 2
                reasons.append("RSI overbought (>70)")
            elif rsi < 40:
                buy_signals += 1
                reasons.append("RSI below 40")
            elif rsi > 60:
                sell_signals += 1
                reasons.append("RSI above 60")

        # MACD Analysis
        if macd:
            macd_val = macd.get("macd")
            signal_val = macd.get("signal")
            histogram = macd.get("histogram")

            if macd_val and signal_val:
                if macd_val > signal_val and histogram and histogram > 0:
                    buy_signals += 2
                    reasons.append("MACD bullish crossover")
                elif macd_val < signal_val and histogram and histogram < 0:
                    sell_signals += 2
                    reasons.append("MACD bearish crossover")
                elif macd_val > signal_val:
                    buy_signals += 1
                    reasons.append("MACD above signal line")
                elif macd_val < signal_val:
                    sell_signals += 1
                    reasons.append("MACD below signal line")

        # Bollinger Bands Analysis
        if bb and current_price:
            position = bb.get("position")
            if position is not None:
                if position < 0.2:
                    buy_signals += 1
                    reasons.append("Price near lower Bollinger Band")
                elif position > 0.8:
                    sell_signals += 1
                    reasons.append("Price near upper Bollinger Band")

        # Moving Average Analysis
        if sma_20 and sma_50 and current_price:
            if sma_20 > sma_50:
                buy_signals += 1
                reasons.append("SMA20 > SMA50 (uptrend)")
            elif sma_20 < sma_50:
                sell_signals += 1
                reasons.append("SMA20 < SMA50 (downtrend)")

            if current_price > sma_20:
                buy_signals += 0.5
            elif current_price < sma_20:
                sell_signals += 0.5

        # Determine final signal
        total_signals = buy_signals + sell_signals
        if total_signals == 0:
            signal = SignalType.HOLD
            confidence = 0.5
            reasons.append("Neutral indicators")
        elif buy_signals > sell_signals:
            signal = SignalType.BUY
            confidence = min(0.95, buy_signals / (total_signals + 1))
            if not reasons:
                reasons.append("Multiple bullish indicators")
        elif sell_signals > buy_signals:
            signal = SignalType.SELL
            confidence = min(0.95, sell_signals / (total_signals + 1))
            if not reasons:
                reasons.append("Multiple bearish indicators")
        else:
            signal = SignalType.HOLD
            confidence = 0.5
            reasons.append("Mixed signals")

        reason = " + ".join(reasons) if reasons else "Insufficient data"

        return signal, round(confidence, 2), reason

    @staticmethod
    async def generate_signals_batch(symbols: List[str]) -> Dict[str, Dict]:
        """
        Generate signals for multiple symbols
        Returns: Dict of symbol -> signal data
        """
        signals = {}
        for symbol in symbols:
            signal = await SignalService.generate_signal(symbol)
            if signal:
                signals[symbol] = signal
            else:
                logger.warning(f"Failed to generate signal for {symbol}")

        return signals
