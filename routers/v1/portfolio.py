"""Portfolio management endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.security import get_current_user
from models.user import User
from schemas.portfolio_schema import (
    PortfolioCreate, PortfolioResponse, PortfolioUpdate, PortfolioWithStats, PortfolioList
)
from services.portfolio_service import PortfolioService

router = APIRouter(prefix="/portfolios", tags=["portfolios"])


@router.post("", response_model=PortfolioResponse, status_code=201)
async def create_portfolio(
    portfolio_data: PortfolioCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Create new portfolio"""
    portfolio = await PortfolioService.create_portfolio(db, current_user.id, portfolio_data)
    return portfolio


@router.get("", response_model=PortfolioList)
async def list_portfolios(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List user's portfolios"""
    portfolios, total = await PortfolioService.list_portfolios(db, current_user.id, skip, limit)
    return PortfolioList(total=total, portfolios=portfolios)


@router.get("/{portfolio_id}", response_model=PortfolioResponse)
async def get_portfolio(
    portfolio_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get specific portfolio"""
    portfolio = await PortfolioService.get_portfolio(db, current_user.id, portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


@router.get("/{portfolio_id}/stats", response_model=dict)
async def get_portfolio_stats(
    portfolio_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get portfolio performance statistics"""
    portfolio = await PortfolioService.get_portfolio(db, current_user.id, portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    stats = await PortfolioService.get_portfolio_stats(db, portfolio_id)
    return stats


@router.put("/{portfolio_id}", response_model=PortfolioResponse)
async def update_portfolio(
    portfolio_id: int,
    portfolio_data: PortfolioUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update portfolio"""
    portfolio = await PortfolioService.update_portfolio(db, current_user.id, portfolio_id, portfolio_data)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return portfolio


@router.delete("/{portfolio_id}", status_code=204)
async def delete_portfolio(
    portfolio_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Delete portfolio"""
    success = await PortfolioService.delete_portfolio(db, current_user.id, portfolio_id)
    if not success:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return None
