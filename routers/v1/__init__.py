"""API v1 routes"""

from fastapi import APIRouter
from .auth import router as auth_router
from .users import router as users_router
from .news import router as news_router
from .technical import router as technical_router
from .waitlist import router as waitlist_router
from .portfolio import router as portfolio_router
from .position import router as position_router
from .signal_history import router as signal_history_router
from .health import router as health_router

router = APIRouter(prefix="/api/v1", tags=["v1"])

# Include routers
router.include_router(auth_router, tags=["authentication"])
router.include_router(users_router, tags=["users"])
router.include_router(news_router, tags=["news"])
router.include_router(technical_router, tags=["technical-analysis"])
router.include_router(waitlist_router, tags=["waitlist"])
router.include_router(portfolio_router, tags=["portfolios"])
router.include_router(position_router, tags=["positions"])
router.include_router(signal_history_router, tags=["signal-history"])
router.include_router(health_router, tags=["health"])
