"""Waitlist request/response schemas"""

from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


class WaitlistSignup(BaseModel):
    """Waitlist signup schema"""
    email: EmailStr
    name: Optional[str] = Field(None, max_length=255)
    source: Optional[str] = Field(default="landing_page", max_length=50)

    class Config:
        from_attributes = True


class WaitlistResponse(BaseModel):
    """Waitlist response schema"""
    id: int
    email: str
    name: Optional[str]
    source: str
    is_notified: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class WaitlistMessage(BaseModel):
    """Simple response message"""
    message: str
    email: str
