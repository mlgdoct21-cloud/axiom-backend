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
from .daily_digest import router as daily_digest_router
from .dashboard_summary import router as dashboard_summary_router
from .etf_admin import router as etf_admin_router
from .macro_admin import router as macro_admin_router
from .macro_public import router as macro_public_router
from .billing import router as billing_router
from .crypto_onchain import router as crypto_onchain_router
from .feature_quota import router as feature_quota_router
from .corporate_admin import router as corporate_admin_router
from .corporate_public import router as corporate_public_router
from .academy import router as academy_router
from .cockpit import router as cockpit_router
from .bist import router as bist_router
from .dossier import router as dossier_router

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
router.include_router(daily_digest_router, tags=["daily-digest"])
router.include_router(dashboard_summary_router, tags=["dashboard-summary"])
router.include_router(etf_admin_router, tags=["admin"])
router.include_router(macro_admin_router, tags=["admin"])
router.include_router(corporate_admin_router, tags=["admin"])
router.include_router(corporate_public_router, tags=["corporate"])
router.include_router(macro_public_router, tags=["macro"])
router.include_router(billing_router, tags=["billing"])
router.include_router(crypto_onchain_router, tags=["crypto"])
router.include_router(feature_quota_router, tags=["feature-quota"])
router.include_router(academy_router, tags=["academy"])
router.include_router(cockpit_router, tags=["cockpit"])
router.include_router(bist_router, tags=["bist"])
router.include_router(dossier_router, tags=["dossier"])
