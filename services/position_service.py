"""Position management service"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from datetime import datetime
from models.position import Position
from models.portfolio import Portfolio
from schemas.position_schema import PositionCreate, PositionUpdate, PositionClose
from core.logger import get_logger

logger = get_logger("position_service")


class PositionService:
    """Service for position operations"""

    @staticmethod
    async def open_position(db: AsyncSession, user_id: int, portfolio_id: int, position_data: PositionCreate) -> Position:
        """Open new position"""
        try:
            # Verify portfolio ownership
            portfolio_query = select(Portfolio).where(
                (Portfolio.id == portfolio_id) & (Portfolio.user_id == user_id)
            )
            portfolio_result = await db.execute(portfolio_query)
            portfolio = portfolio_result.scalars().first()

            if not portfolio:
                logger.warning(f"Portfolio {portfolio_id} not found for user {user_id}")
                return None

            position = Position(
                portfolio_id=portfolio_id,
                symbol=position_data.symbol.upper(),
                quantity=position_data.quantity,
                average_entry_price=position_data.average_entry_price,
                current_price=position_data.average_entry_price,
                entry_date=position_data.entry_date or datetime.now(),
                status="open"
            )
            db.add(position)
            await db.commit()
            await db.refresh(position)
            logger.info(f"Opened position {position.id}: {position.symbol} x{position.quantity}")
            return position
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to open position: {e}")
            raise

    @staticmethod
    async def get_position(db: AsyncSession, user_id: int, position_id: int) -> Position:
        """Get position by ID (verify ownership through portfolio)"""
        query = select(Position).join(Portfolio).where(
            (Position.id == position_id) & (Portfolio.user_id == user_id)
        )
        result = await db.execute(query)
        return result.scalars().first()

    @staticmethod
    async def list_positions(db: AsyncSession, user_id: int, portfolio_id: int, status: str = None) -> list[Position]:
        """List positions in portfolio"""
        query = select(Position).join(Portfolio).where(
            (Position.portfolio_id == portfolio_id) & (Portfolio.user_id == user_id)
        )

        if status:
            query = query.where(Position.status == status)

        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def list_open_positions(db: AsyncSession, portfolio_id: int) -> list[Position]:
        """List open positions in portfolio"""
        query = select(Position).where(
            (Position.portfolio_id == portfolio_id) & (Position.status == "open")
        )
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def list_closed_positions(db: AsyncSession, portfolio_id: int) -> list[Position]:
        """List closed positions in portfolio"""
        query = select(Position).where(
            (Position.portfolio_id == portfolio_id) & (Position.status == "closed")
        )
        result = await db.execute(query)
        return result.scalars().all()

    @staticmethod
    async def update_position_price(db: AsyncSession, user_id: int, position_id: int, current_price: float) -> Position:
        """Update position current price"""
        try:
            position = await PositionService.get_position(db, user_id, position_id)
            if not position:
                return None

            position.current_price = current_price
            await db.commit()
            await db.refresh(position)
            logger.info(f"Updated position {position_id} price to {current_price}")
            return position
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to update position price: {e}")
            raise

    @staticmethod
    def calculate_pnl(position: Position) -> tuple[float, float]:
        """Calculate P&L for a position"""
        pnl_amount = (position.current_price - position.average_entry_price) * position.quantity
        if position.average_entry_price == 0:
            pnl_percent = 0
        else:
            pnl_percent = ((position.current_price - position.average_entry_price) / position.average_entry_price) * 100
        return pnl_amount, pnl_percent

    @staticmethod
    async def close_position(db: AsyncSession, user_id: int, position_id: int, position_data: PositionClose) -> Position:
        """Close position"""
        try:
            position = await PositionService.get_position(db, user_id, position_id)
            if not position:
                return None

            position.status = "closed"
            position.exit_date = position_data.exit_date or datetime.now()
            position.current_price = position_data.exit_price or position.current_price

            await db.commit()
            await db.refresh(position)
            logger.info(f"Closed position {position_id}")
            return position
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to close position: {e}")
            raise

    @staticmethod
    async def delete_position(db: AsyncSession, user_id: int, position_id: int) -> bool:
        """Delete position (only open positions)"""
        try:
            position = await PositionService.get_position(db, user_id, position_id)
            if not position or position.status != "open":
                return False

            await db.delete(position)
            await db.commit()
            logger.info(f"Deleted position {position_id}")
            return True
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to delete position: {e}")
            raise
