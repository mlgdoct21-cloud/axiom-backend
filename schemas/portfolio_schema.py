"""Portfolio request/response schemas"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class PortfolioCreate(BaseModel):
    """Create new portfolio"""
    name: str = Field(..., min_length=1, max_length=255)
    currency: str = Field(default="USD", max_length=10)
    initial_balance: float = Field(..., gt=0)

    class Config:
        from_attributes = True


class PortfolioUpdate(BaseModel):
    """Update portfolio"""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    currency: Optional[str] = Field(None, max_length=10)
    initial_balance: Optional[float] = Field(None, gt=0)

    class Config:
        from_attributes = True


class PortfolioResponse(BaseModel):
    """Portfolio response"""
    id: int
    user_id: int
    name: str
    currency: str
    initial_balance: float
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PortfolioWithStats(PortfolioResponse):
    """Portfolio with performance stats"""
    current_value: float  # Sum of all open positions
    total_pnl_amount: float  # Total P&L across all positions
    total_pnl_percent: float  # Total P&L percentage
    open_positions_count: int
    closed_positions_count: int


class PortfolioList(BaseModel):
    """List of portfolios"""
    total: int
    portfolios: List[PortfolioResponse]

    class Config:
        from_attributes = True
