"""Portfolio management service"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, delete as delete_stmt
from models.portfolio import Portfolio
from models.position import Position
from schemas.portfolio_schema import PortfolioCreate, PortfolioUpdate
from core.logger import get_logger

logger = get_logger("portfolio_service")


class PortfolioService:
    """Service for portfolio operations"""

    @staticmethod
    async def create_portfolio(db: AsyncSession, user_id: int, portfolio_data: PortfolioCreate) -> Portfolio:
        """Create new portfolio"""
        try:
            portfolio = Portfolio(
                user_id=user_id,
                name=portfolio_data.name,
                currency=portfolio_data.currency,
                initial_balance=portfolio_data.initial_balance
            )
            db.add(portfolio)
            await db.commit()
            await db.refresh(portfolio)
            logger.info(f"Created portfolio {portfolio.id} for user {user_id}")
            return portfolio
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to create portfolio: {e}")
            raise

    @staticmethod
    async def get_portfolio(db: AsyncSession, user_id: int, portfolio_id: int) -> Portfolio:
        """Get portfolio by ID (verify ownership)"""
        query = select(Portfolio).where(
            (Portfolio.id == portfolio_id) & (Portfolio.user_id == user_id)
        )
        result = await db.execute(query)
        return result.scalars().first()

    @staticmethod
    async def list_portfolios(db: AsyncSession, user_id: int, skip: int = 0, limit: int = 100) -> tuple[list[Portfolio], int]:
        """List user's portfolios"""
        # Get total count
        count_query = select(func.count(Portfolio.id)).where(Portfolio.user_id == user_id)
        total_result = await db.execute(count_query)
        total = total_result.scalar()

        # Get portfolios with offset and limit
        query = select(Portfolio).where(
            Portfolio.user_id == user_id
        ).offset(skip).limit(limit)
        result = await db.execute(query)
        portfolios = result.scalars().all()

        return portfolios, total

    @staticmethod
    async def update_portfolio(db: AsyncSession, user_id: int, portfolio_id: int, portfolio_data: PortfolioUpdate) -> Portfolio:
        """Update portfolio"""
        try:
            portfolio = await PortfolioService.get_portfolio(db, user_id, portfolio_id)
            if not portfolio:
                return None

            if portfolio_data.name:
                portfolio.name = portfolio_data.name
            if portfolio_data.currency:
                portfolio.currency = portfolio_data.currency
            if portfolio_data.initial_balance:
                portfolio.initial_balance = portfolio_data.initial_balance

            await db.commit()
            await db.refresh(portfolio)
            logger.info(f"Updated portfolio {portfolio_id}")
            return portfolio
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to update portfolio: {e}")
            raise

    @staticmethod
    async def delete_portfolio(db: AsyncSession, user_id: int, portfolio_id: int) -> bool:
        """Delete portfolio"""
        try:
            portfolio = await PortfolioService.get_portfolio(db, user_id, portfolio_id)
            if not portfolio:
                return False

            await db.delete(portfolio)
            await db.commit()
            logger.info(f"Deleted portfolio {portfolio_id}")
            return True
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to delete portfolio: {e}")
            raise

    @staticmethod
    async def calculate_portfolio_value(db: AsyncSession, portfolio_id: int) -> float:
        """Calculate current portfolio value (sum of all open positions)"""
        query = select(Position).where(
            (Position.portfolio_id == portfolio_id) & (Position.status == "open")
        )
        result = await db.execute(query)
        positions = result.scalars().all()

        total_value = sum(pos.current_price * pos.quantity for pos in positions)
        return total_value

    @staticmethod
    async def calculate_portfolio_pnl(db: AsyncSession, portfolio_id: int) -> tuple[float, float]:
        """Calculate portfolio P&L (amount and percentage)"""
        portfolio_query = select(Portfolio).where(Portfolio.id == portfolio_id)
        portfolio_result = await db.execute(portfolio_query)
        portfolio = portfolio_result.scalars().first()

        if not portfolio:
            return 0, 0

        positions_query = select(Position).where(Position.portfolio_id == portfolio_id)
        positions_result = await db.execute(positions_query)
        positions = positions_result.scalars().all()

        total_pnl_amount = sum(
            (pos.current_price - pos.average_entry_price) * pos.quantity
            for pos in positions
        )

        if portfolio.initial_balance == 0:
            return total_pnl_amount, 0

        total_pnl_percent = (total_pnl_amount / portfolio.initial_balance) * 100

        return total_pnl_amount, total_pnl_percent

    @staticmethod
    async def get_portfolio_stats(db: AsyncSession, portfolio_id: int) -> dict:
        """Get detailed portfolio statistics"""
        portfolio_query = select(Portfolio).where(Portfolio.id == portfolio_id)
        portfolio_result = await db.execute(portfolio_query)
        portfolio = portfolio_result.scalars().first()

        if not portfolio:
            return None

        current_value = await PortfolioService.calculate_portfolio_value(db, portfolio_id)
        pnl_amount, pnl_percent = await PortfolioService.calculate_portfolio_pnl(db, portfolio_id)

        open_count_query = select(func.count(Position.id)).where(
            (Position.portfolio_id == portfolio_id) & (Position.status == "open")
        )
        open_count_result = await db.execute(open_count_query)
        open_positions = open_count_result.scalar()

        closed_count_query = select(func.count(Position.id)).where(
            (Position.portfolio_id == portfolio_id) & (Position.status == "closed")
        )
        closed_count_result = await db.execute(closed_count_query)
        closed_positions = closed_count_result.scalar()

        return {
            "portfolio_id": portfolio_id,
            "name": portfolio.name,
            "currency": portfolio.currency,
            "initial_balance": portfolio.initial_balance,
            "current_value": current_value,
            "total_pnl_amount": pnl_amount,
            "total_pnl_percent": pnl_percent,
            "open_positions_count": open_positions,
            "closed_positions_count": closed_positions,
            "total_positions": open_positions + closed_positions
        }
