"""User request/response schemas"""

from pydantic import BaseModel, EmailStr, Field, validator
from typing import Optional
from datetime import datetime


class UserCreate(BaseModel):
    """User registration schema"""
    telegram_id: str = Field(..., min_length=1, max_length=20)
    username: Optional[str] = Field(None, max_length=32)

    class Config:
        from_attributes = True


class UserLogin(BaseModel):
    """User login schema"""
    telegram_id: str = Field(..., min_length=1, max_length=20)

    class Config:
        from_attributes = True


class UserSettings(BaseModel):
    """User settings update schema"""
    tags: str = Field(default="", max_length=500)
    report_mode: str = Field(default="digest", pattern="^(realtime|digest)$")
    report_hours: str = Field(default="08:00", max_length=100)
    custom_follows: str = Field(default="", max_length=1000)

    @validator('tags', 'custom_follows')
    def validate_comma_separated(cls, v):
        if v:
            items = [item.strip() for item in v.split(',') if item.strip()]
            if len(items) > 20:
                raise ValueError('Maximum 20 items allowed')
        return v

    class Config:
        from_attributes = True


class UserResponse(BaseModel):
    """User response schema"""
    id: int
    telegram_id: str
    username: Optional[str]
    is_active: bool
    tier: str = "free"
    tags: str
    report_mode: str
    report_hours: str
    custom_follows: str
    subscription_status: Optional[str] = None
    current_period_end: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    """User update schema"""
    username: Optional[str] = Field(None, max_length=32)
    tags: Optional[str] = Field(None, max_length=500)
    report_mode: Optional[str] = Field(None, pattern="^(realtime|digest)$")
    report_hours: Optional[str] = Field(None, max_length=100)
    custom_follows: Optional[str] = Field(None, max_length=1000)

    class Config:
        from_attributes = True
