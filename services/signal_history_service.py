"""Signal history tracking service"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import desc, select, func
from datetime import datetime, timedelta
from models.signal_history import SignalHistory
from schemas.signal_history_schema import SignalCreate, SignalActionUpdate
from core.logger import get_logger

logger = get_logger("signal_history_service")


class SignalHistoryService:
    """Service for signal history operations"""

    @staticmethod
    async def record_signal(
        db: AsyncSession,
        user_id: int,
        symbol: str,
        signal_type: str,
        confidence: float,
        reasoning: str,
        price_at_signal: float,
        indicators: dict = None
    ) -> SignalHistory:
        """Record signal to history"""
        try:
            signal = SignalHistory(
                user_id=user_id,
                symbol=symbol.upper(),
                signal_type=signal_type.upper(),
                confidence=confidence,
                reasoning=reasoning,
                indicators=indicators,
                price_at_signal=price_at_signal,
                action_taken=False
            )
            db.add(signal)
            await db.commit()
            await db.refresh(signal)
            logger.info(f"Recorded {signal_type} signal for {symbol} @ {confidence*100:.0f}% confidence")
            return signal
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to record signal: {e}")
            raise

    @staticmethod
    async def get_signal_history(db: AsyncSession, user_id: int, symbol: str = None, limit: int = 100, skip: int = 0) -> tuple[list[SignalHistory], int]:
        """Get signal history for user"""
        query = select(SignalHistory).where(SignalHistory.user_id == user_id)

        if symbol:
            query = query.where(SignalHistory.symbol == symbol.upper())

        # Get total count
        count_query = select(func.count(SignalHistory.id)).where(SignalHistory.user_id == user_id)
        if symbol:
            count_query = count_query.where(SignalHistory.symbol == symbol.upper())
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Get signals
        query = query.order_by(desc(SignalHistory.timestamp)).offset(skip).limit(limit)
        result = await db.execute(query)
        signals = result.scalars().all()

        return signals, total

    @staticmethod
    async def get_signals_for_period(
        db: AsyncSession,
        user_id: int,
        start_date: datetime,
        end_date: datetime,
        symbol: str = None
    ) -> list[SignalHistory]:
        """Get signals within date range"""
        query = select(SignalHistory).where(
            (SignalHistory.user_id == user_id) &
            (SignalHistory.timestamp >= start_date) &
            (SignalHistory.timestamp <= end_date)
        )

        if symbol:
            query = query.where(SignalHistory.symbol == symbol.upper())

        query = query.order_by(desc(SignalHistory.timestamp))
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def mark_signal_action(
        db: AsyncSession,
        user_id: int,
        signal_id: int,
        action_data: SignalActionUpdate
    ) -> SignalHistory:
        """Mark signal as acted upon"""
        try:
            signal_query = select(SignalHistory).where(
                (SignalHistory.id == signal_id) & (SignalHistory.user_id == user_id)
            )
            result = await db.execute(signal_query)
            signal = result.scalars().first()

            if not signal:
                return None

            signal.action_taken = action_data.action_taken
            await db.commit()
            await db.refresh(signal)
            logger.info(f"Updated signal {signal_id} action status")
            return signal
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to mark signal action: {e}")
            raise

    @staticmethod
    async def link_signal_to_position(
        db: AsyncSession,
        user_id: int,
        signal_id: int,
        position_id: int
    ) -> SignalHistory:
        """Link signal to a position"""
        try:
            signal_query = select(SignalHistory).where(
                (SignalHistory.id == signal_id) & (SignalHistory.user_id == user_id)
            )
            result = await db.execute(signal_query)
            signal = result.scalars().first()

            if not signal:
                return None

            signal.position_id = position_id
            signal.action_taken = True
            await db.commit()
            await db.refresh(signal)
            logger.info(f"Linked signal {signal_id} to position {position_id}")
            return signal
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to link signal to position: {e}")
            raise

    @staticmethod
    async def calculate_signal_stats(db: AsyncSession, user_id: int) -> dict:
        """Calculate signal accuracy statistics"""
        query = select(SignalHistory).where(SignalHistory.user_id == user_id)
        result = await db.execute(query)
        signals = result.scalars().all()

        if not signals:
            return {
                "total_signals": 0,
                "buy_signals": 0,
                "sell_signals": 0,
                "hold_signals": 0,
                "acted_upon": 0,
                "win_rate": 0,
                "avg_confidence": 0,
                "most_traded_symbol": None
            }

        total_signals = len(signals)
        buy_signals = sum(1 for s in signals if s.signal_type == "BUY")
        sell_signals = sum(1 for s in signals if s.signal_type == "SELL")
        hold_signals = sum(1 for s in signals if s.signal_type == "HOLD")
        acted_upon = sum(1 for s in signals if s.action_taken)
        avg_confidence = sum(s.confidence for s in signals) / total_signals if total_signals > 0 else 0

        # Most traded symbol
        symbol_counts = {}
        for signal in signals:
            symbol_counts[signal.symbol] = symbol_counts.get(signal.symbol, 0) + 1
        most_traded_symbol = max(symbol_counts, key=symbol_counts.get) if symbol_counts else None

        return {
            "total_signals": total_signals,
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
            "hold_signals": hold_signals,
            "acted_upon": acted_upon,
            "win_rate": (acted_upon / total_signals * 100) if total_signals > 0 else 0,
            "avg_confidence": avg_confidence,
            "most_traded_symbol": most_traded_symbol
        }

    @staticmethod
    async def get_signal(db: AsyncSession, user_id: int, signal_id: int) -> SignalHistory:
        """Get specific signal"""
        query = select(SignalHistory).where(
            (SignalHistory.id == signal_id) & (SignalHistory.user_id == user_id)
        )
        result = await db.execute(query)
        return result.scalars().first()
