"""Signal history endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta

from core.database import get_db
from core.security import get_current_user
from models.user import User
from schemas.signal_history_schema import (
    SignalHistoryResponse, SignalHistoryList, SignalStats, SignalActionUpdate
)
from services.signal_history_service import SignalHistoryService

router = APIRouter(prefix="/signal-history", tags=["signal-history"])


@router.get("", response_model=SignalHistoryList)
async def get_signal_history(
    symbol: str = None,
    limit: int = 100,
    skip: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get signal history for user"""
    signals, total = await SignalHistoryService.get_signal_history(
        db, current_user.id, symbol=symbol, limit=limit, skip=skip
    )
    return SignalHistoryList(total=total, signals=signals)


@router.get("/symbol/{symbol}", response_model=SignalHistoryList)
async def get_signals_by_symbol(
    symbol: str,
    limit: int = 50,
    skip: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get signal history for specific symbol"""
    signals, total = await SignalHistoryService.get_signal_history(
        db, current_user.id, symbol=symbol, limit=limit, skip=skip
    )
    return SignalHistoryList(total=total, signals=signals)


@router.get("/period/{days}", response_model=SignalHistoryList)
async def get_signals_for_period(
    days: int = 7,
    symbol: str = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get signals from last X days"""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    signals = await SignalHistoryService.get_signals_for_period(
        db, current_user.id, start_date, end_date, symbol=symbol
    )
    return SignalHistoryList(total=len(signals), signals=signals)


@router.get("/stats", response_model=SignalStats)
async def get_signal_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get signal accuracy statistics"""
    stats = await SignalHistoryService.calculate_signal_stats(db, current_user.id)
    return SignalStats(**stats)


@router.post("/{signal_id}/action", response_model=SignalHistoryResponse)
async def mark_signal_action(
    signal_id: int,
    action_data: SignalActionUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Mark signal as acted upon or link to position"""
    signal = await SignalHistoryService.mark_signal_action(db, current_user.id, signal_id, action_data)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal


@router.post("/{signal_id}/link-position", response_model=SignalHistoryResponse)
async def link_signal_to_position(
    signal_id: int,
    position_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Link signal to a position"""
    signal = await SignalHistoryService.link_signal_to_position(db, current_user.id, signal_id, position_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal


@router.get("/{signal_id}", response_model=SignalHistoryResponse)
async def get_signal(
    signal_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get specific signal"""
    signal = await SignalHistoryService.get_signal(db, current_user.id, signal_id)
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    return signal
