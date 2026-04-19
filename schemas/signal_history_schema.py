"""Signal history request/response schemas"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class SignalCreate(BaseModel):
    """Create signal history record"""
    user_id: int
    symbol: str = Field(..., min_length=1, max_length=20)
    signal_type: str = Field(..., pattern="^(BUY|SELL|HOLD)$")
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str = Field(..., min_length=1)
    indicators: Optional[Dict[str, Any]] = None
    price_at_signal: float = Field(..., gt=0)

    class Config:
        from_attributes = True


class SignalActionUpdate(BaseModel):
    """Mark signal as acted upon"""
    action_taken: bool
    position_id: Optional[int] = None

    class Config:
        from_attributes = True


class SignalHistoryResponse(BaseModel):
    """Signal history response"""
    id: int
    user_id: int
    symbol: str
    signal_type: str
    confidence: float
    reasoning: str
    indicators: Optional[Dict[str, Any]] = None
    price_at_signal: float
    action_taken: bool
    position_id: Optional[int] = None
    timestamp: datetime

    class Config:
        from_attributes = True


class SignalHistoryList(BaseModel):
    """List of signal history"""
    total: int
    signals: List[SignalHistoryResponse]

    class Config:
        from_attributes = True


class SignalStats(BaseModel):
    """Signal accuracy statistics"""
    total_signals: int
    buy_signals: int
    sell_signals: int
    hold_signals: int
    acted_upon: int
    win_rate: float  # % of signals that made money
    avg_confidence: float
    most_traded_symbol: Optional[str] = None

    class Config:
        from_attributes = True
