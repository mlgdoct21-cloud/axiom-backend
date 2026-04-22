"""News request/response schemas"""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class NewsCreate(BaseModel):
    """News creation schema"""
    source: str = Field(..., max_length=255)
    original_title: str = Field(..., max_length=500)
    original_link: str = Field(..., max_length=1000)
    ai_summary: Optional[str] = None
    ai_tags: Optional[str] = Field(None, max_length=500)
    telegram_hook: Optional[str] = None
    dashboard_summary: Optional[str] = None
    axiom_analysis: Optional[str] = None

    class Config:
        from_attributes = True


class NewsResponse(BaseModel):
    """News response schema"""
    id: int
    source: str
    original_title: str
    original_link: str
    ai_summary: Optional[str]
    ai_tags: Optional[str]
    telegram_hook: Optional[str]
    dashboard_summary: Optional[str]
    axiom_analysis: Optional[str]
    symbol: Optional[str] = None
    analyzed: Optional[bool] = None
    is_urgent: Optional[bool] = None
    created_at: datetime

    class Config:
        from_attributes = True


class NewsFilter(BaseModel):
    """News filtering parameters"""
    skip: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
    source: Optional[str] = None
    tag: Optional[str] = None
    search: Optional[str] = None

    class Config:
        from_attributes = True
