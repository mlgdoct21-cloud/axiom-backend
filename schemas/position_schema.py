"""Position request/response schemas"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class PositionCreate(BaseModel):
    """Open new position"""
    symbol: str = Field(..., min_length=1, max_length=20)
    quantity: float = Field(..., gt=0)
    average_entry_price: float = Field(..., gt=0)
    entry_date: Optional[datetime] = None  # Defaults to now

    class Config:
        from_attributes = True


class PositionClose(BaseModel):
    """Close position"""
    exit_date: Optional[datetime] = None  # Defaults to now
    current_price: Optional[float] = Field(None, gt=0)  # Final price if different

    class Config:
        from_attributes = True


class PositionUpdate(BaseModel):
    """Update position (mainly for current_price)"""
    current_price: Optional[float] = Field(None, gt=0)
    quantity: Optional[float] = Field(None, gt=0)

    class Config:
        from_attributes = True


class PositionResponse(BaseModel):
    """Position response"""
    id: int
    portfolio_id: int
    symbol: str
    quantity: float
    average_entry_price: float
    current_price: float
    entry_date: datetime
    exit_date: Optional[datetime] = None
    status: str  # "open" or "closed"
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PositionWithPnL(PositionResponse):
    """Position with P&L calculations"""
    pnl_amount: float
    pnl_percent: float


class PositionList(BaseModel):
    """List of positions"""
    total: int
    positions: List[PositionResponse]

    class Config:
        from_attributes = True
