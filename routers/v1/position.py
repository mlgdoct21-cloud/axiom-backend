"""Position management endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.security import get_current_user
from models.user import User
from schemas.position_schema import (
    PositionCreate, PositionResponse, PositionUpdate, PositionClose, PositionWithPnL, PositionList
)
from services.position_service import PositionService
from services.portfolio_service import PortfolioService

router = APIRouter(prefix="/portfolios/{portfolio_id}/positions", tags=["positions"])


@router.post("", response_model=PositionResponse, status_code=201)
async def open_position(
    portfolio_id: int,
    position_data: PositionCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Open new position"""
    position = await PositionService.open_position(db, current_user.id, portfolio_id, position_data)
    if not position:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return position


@router.get("", response_model=PositionList)
async def list_positions(
    portfolio_id: int,
    status_filter: str = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """List positions in portfolio"""
    # Verify portfolio ownership
    portfolio = await PortfolioService.get_portfolio(db, current_user.id, portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    positions = await PositionService.list_positions(db, current_user.id, portfolio_id, status_filter)

    # Add P&L calculations
    positions_with_pnl = []
    for pos in positions:
        pnl_amount, pnl_percent = PositionService.calculate_pnl(pos)
        positions_with_pnl.append({
            **pos.__dict__,
            "pnl_amount": pnl_amount,
            "pnl_percent": pnl_percent
        })

    return PositionList(total=len(positions_with_pnl), positions=positions_with_pnl)


@router.get("/{position_id}", response_model=PositionWithPnL)
async def get_position(
    portfolio_id: int,
    position_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get specific position"""
    position = await PositionService.get_position(db, current_user.id, position_id)
    if not position or position.portfolio_id != portfolio_id:
        raise HTTPException(status_code=404, detail="Position not found")

    pnl_amount, pnl_percent = PositionService.calculate_pnl(position)
    return {
        **position.__dict__,
        "pnl_amount": pnl_amount,
        "pnl_percent": pnl_percent
    }


@router.put("/{position_id}", response_model=PositionResponse)
async def update_position(
    portfolio_id: int,
    position_id: int,
    position_data: PositionUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Update position (current price, quantity)"""
    position = await PositionService.get_position(db, current_user.id, position_id)
    if not position or position.portfolio_id != portfolio_id:
        raise HTTPException(status_code=404, detail="Position not found")

    if position_data.current_price:
        position = await PositionService.update_position_price(db, current_user.id, position_id, position_data.current_price)

    return position


@router.post("/{position_id}/close", response_model=PositionResponse)
async def close_position(
    portfolio_id: int,
    position_id: int,
    position_data: PositionClose,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Close position"""
    position = await PositionService.get_position(db, current_user.id, position_id)
    if not position or position.portfolio_id != portfolio_id:
        raise HTTPException(status_code=404, detail="Position not found")

    position = await PositionService.close_position(db, current_user.id, position_id, position_data)
    return position


@router.delete("/{position_id}", status_code=204)
async def delete_position(
    portfolio_id: int,
    position_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Delete position (only open positions)"""
    success = await PositionService.delete_position(db, current_user.id, position_id)
    if not success:
        raise HTTPException(status_code=404, detail="Position not found or already closed")
    return None
