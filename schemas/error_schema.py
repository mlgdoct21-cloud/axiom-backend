"""Error response schemas"""

from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class ErrorResponse(BaseModel):
    """Standardized error response"""
    error: str
    message: str
    status_code: int
    timestamp: str
    path: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True
