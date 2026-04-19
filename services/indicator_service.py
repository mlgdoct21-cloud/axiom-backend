"""Technical Indicators Service - MACD, RSI, Bollinger Bands, SMA"""

from typing import List, Dict, Optional, Tuple
from core.logger import get_logger

logger = get_logger("indicator_service")


class IndicatorService:
    """Service for calculating technical indicators"""

    @staticmethod
    def calculate_sma(prices: List[float], period: int = 20) -> Optional[List[float]]:
        """
        Calculate Simple Moving Average
        Returns: List of SMA values
        """
        if len(prices) < period:
            return None

        sma_values = []
        for i in range(len(prices)):
            if i < period - 1:
                sma_values.append(None)
            else:
                window = prices[i - period + 1:i + 1]
                sma = sum(window) / period
                sma_values.append(sma)

        return sma_values

    @staticmethod
    def calculate_ema(prices: List[float], period: int = 12) -> Optional[List[float]]:
        """
        Calculate Exponential Moving Average
        Returns: List of EMA values
        """
        if len(prices) < period:
            return None

        multiplier = 2 / (period + 1)
        ema_values = []
        sma = sum(prices[:period]) / period

        for i, price in enumerate(prices):
            if i < period - 1:
                ema_values.append(None)
            elif i == period - 1:
                ema_values.append(sma)
            else:
                ema = (price - ema_values[-1]) * multiplier + ema_values[-1]
                ema_values.append(ema)

        return ema_values

    @staticmethod
    def calculate_macd(
        prices: List[float],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> Optional[Dict]:
        """
        Calculate MACD (Moving Average Convergence Divergence)
        Returns: {
            "macd": List[float],
            "signal": List[float],
            "histogram": List[float],
            "last_value": {"macd": float, "signal": float, "histogram": float}
        }
        """
        try:
            if len(prices) < slow_period:
                return None

            # Calculate EMAs
            fast_ema = IndicatorService.calculate_ema(prices, fast_period)
            slow_ema = IndicatorService.calculate_ema(prices, slow_period)

            if not fast_ema or not slow_ema:
                return None

            # Calculate MACD line
            macd_line = []
            for f, s in zip(fast_ema, slow_ema):
                if f is None or s is None:
                    macd_line.append(None)
                else:
                    macd_line.append(f - s)

            # Calculate signal line (EMA of MACD)
            valid_macd = [m for m in macd_line if m is not None]
            if len(valid_macd) < signal_period:
                return None

            signal_line = IndicatorService.calculate_ema(valid_macd, signal_period)

            # Calculate histogram
            histogram = []
            signal_idx = 0
            for i, macd_val in enumerate(macd_line):
                if macd_val is None:
                    histogram.append(None)
                else:
                    if signal_idx < len(signal_line) and signal_line[signal_idx] is not None:
                        histogram.append(macd_val - signal_line[signal_idx])
                        signal_idx += 1
                    else:
                        histogram.append(None)

            # Get last values
            last_macd = next((m for m in reversed(macd_line) if m is not None), None)
            last_signal = next((s for s in reversed(signal_line) if s is not None), None)
            last_histogram = next((h for h in reversed(histogram) if h is not None), None)

            return {
                "macd": macd_line,
                "signal": signal_line,
                "histogram": histogram,
                "last_value": {
                    "macd": round(last_macd, 4) if last_macd else None,
                    "signal": round(last_signal, 4) if last_signal else None,
                    "histogram": round(last_histogram, 4) if last_histogram else None
                }
            }

        except Exception as e:
            logger.error(f"Error calculating MACD: {e}")
            return None

    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
        """
        Calculate RSI (Relative Strength Index)
        Returns: Single RSI value (0-100)
        """
        try:
            if len(prices) < period + 1:
                return None

            deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

            seed = deltas[:period]
            up = sum([d for d in seed if d > 0]) / period
            down = sum([-d for d in seed if d < 0]) / period

            rs = up / down if down != 0 else 0
            rsi = 100 - (100 / (1 + rs)) if rs != 0 else 50

            # Smooth RSI for remaining deltas
            for delta in deltas[period:]:
                up = (up * (period - 1) + (delta if delta > 0 else 0)) / period
                down = (down * (period - 1) + (-delta if delta < 0 else 0)) / period
                rs = up / down if down != 0 else 0
                rsi = 100 - (100 / (1 + rs)) if rs != 0 else 50

            return round(rsi, 2)

        except Exception as e:
            logger.error(f"Error calculating RSI: {e}")
            return None

    @staticmethod
    def calculate_bollinger_bands(
        prices: List[float],
        period: int = 20,
        num_std_dev: float = 2.0
    ) -> Optional[Dict]:
        """
        Calculate Bollinger Bands
        Returns: {
            "upper": float,
            "middle": float,
            "lower": float,
            "position": float (0-1, where 1 is at upper band)
        }
        """
        try:
            if len(prices) < period:
                return None

            # Calculate SMA
            recent_prices = prices[-period:]
            middle = sum(recent_prices) / period

            # Calculate standard deviation
            variance = sum((x - middle) ** 2 for x in recent_prices) / period
            std_dev = variance ** 0.5

            upper = middle + (num_std_dev * std_dev)
            lower = middle - (num_std_dev * std_dev)

            # Calculate position (0-1)
            current_price = prices[-1]
            if upper == lower:
                position = 0.5
            else:
                position = (current_price - lower) / (upper - lower)
                position = max(0, min(1, position))  # Clamp 0-1

            return {
                "upper": round(upper, 2),
                "middle": round(middle, 2),
                "lower": round(lower, 2),
                "position": round(position, 2),
                "current_price": round(current_price, 2)
            }

        except Exception as e:
            logger.error(f"Error calculating Bollinger Bands: {e}")
            return None

    @staticmethod
    def calculate_all_indicators(prices: List[float]) -> Optional[Dict]:
        """
        Calculate all indicators at once
        Returns: Complete technical analysis
        """
        try:
            if len(prices) < 30:
                logger.warning("Not enough price data for indicators")
                return None

            rsi = IndicatorService.calculate_rsi(prices)
            macd = IndicatorService.calculate_macd(prices)
            bb = IndicatorService.calculate_bollinger_bands(prices)
            sma_20 = IndicatorService.calculate_sma(prices, 20)
            sma_50 = IndicatorService.calculate_sma(prices, 50)

            last_sma_20 = next((s for s in reversed(sma_20) if s is not None), None) if sma_20 else None
            last_sma_50 = next((s for s in reversed(sma_50) if s is not None), None) if sma_50 else None

            return {
                "rsi": rsi,
                "macd": macd,
                "bollinger_bands": bb,
                "sma_20": round(last_sma_20, 2) if last_sma_20 else None,
                "sma_50": round(last_sma_50, 2) if last_sma_50 else None,
                "current_price": round(prices[-1], 2)
            }

        except Exception as e:
            logger.error(f"Error calculating all indicators: {e}")
            return None
