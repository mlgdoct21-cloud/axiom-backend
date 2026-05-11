from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging
import os

from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware

from core.database import engine
import models  # Import models so SQLAlchemy knows about them
from routers.v1 import router as v1_router
from services.telegram_bot import start_telegram_bot
from services.crawler import run_crawler
from services.etf_flow_scheduler import etf_scraper_supervisor
from services.coinglass_scheduler import coinglass_scraper_supervisor
from services.macro_sources.reliability_probe import reliability_probe_supervisor
from services.cryptoquant_scheduler import cryptoquant_supervisor
from core.logger import get_logger
from core.schema_guard import ensure_schema
from core.rate_limit import limiter, RateLimitExceeded

logger = get_logger("main")

# References to background tasks
bot_task = None
crawler_task = None
etf_scraper_task = None
coinglass_task = None
macro_probe_task = None
cryptoquant_task = None


async def bot_supervisor():
    """Telegram botunun herhangi bir sebeple çökmesi durumunda tekrar başlatılmasını sağlar."""
    while True:
        try:
            logger.info("Telegram bot supervisor baslatiliyor...")
            await start_telegram_bot()
        except asyncio.CancelledError:
            logger.info("Bot supervisor iptal edildi.")
            break
        except Exception as e:
            logger.error(f"Bot tamamen coktu: {e}. 10 saniye icinde yeniden baslatiliyor...")
            await asyncio.sleep(10)

async def crawler_supervisor():
    """Crawler çökmesi durumunda tekrar başlatılmasını sağlar."""
    while True:
        try:
            logger.info("Crawler supervisor baslatiliyor...")
            await run_crawler()
        except asyncio.CancelledError:
            logger.info("Crawler supervisor iptal edildi.")
            break
        except Exception as e:
            logger.error(f"Crawler tamamen coktu: {e}. 30 saniye icinde yeniden baslatiliyor...")
            await asyncio.sleep(30)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_task, crawler_task, etf_scraper_task, coinglass_task, macro_probe_task, cryptoquant_task
    logger.info("Application startup")

    # Schema guard — idempotent ALTER TABLE to ensure pipeline columns exist.
    # Must run BEFORE crawler/bot so they can write to new columns without errors.
    try:
        await ensure_schema()
    except Exception as e:
        logger.warning(f"Schema guard startup hatası (uygulama yine de başlıyor): {e}")

    # Start Telegram bot in background within a supervisor
    try:
        bot_task = asyncio.create_task(bot_supervisor())
        logger.info("Telegram bot supervisor started")
    except Exception as e:
        logger.error(f"Failed to start bot supervisor: {e}")

    # Start RSS crawler + broadcaster in background
    try:
        crawler_task = asyncio.create_task(crawler_supervisor())
        logger.info("Crawler supervisor started")
    except Exception as e:
        logger.error(f"Failed to start crawler supervisor: {e}")

    # Start ETF flow scraper (daily, scrapes coinglass + fallback chain)
    try:
        etf_scraper_task = asyncio.create_task(etf_scraper_supervisor())
        logger.info("ETF flow scraper started")
    except Exception as e:
        logger.error(f"Failed to start ETF scraper: {e}")

    # CoinGlass Playwright supervisor — 6h cadence, BTC + ETH fresh data.
    # Replaces the unreliable .github/workflows/coinglass-etf-cron.yml
    # (May 6 06:00 UTC schedule run was deferred 2.5h, leaving stale modal).
    try:
        coinglass_task = asyncio.create_task(coinglass_scraper_supervisor())
        logger.info("CoinGlass scheduler started")
    except Exception as e:
        logger.error(f"Failed to start CoinGlass scheduler: {e}")

    # Start Macro source reliability probe (5-min cadence, Hafta 1 verification)
    try:
        macro_probe_task = asyncio.create_task(reliability_probe_supervisor())
        logger.info("Macro reliability probe started")
    except Exception as e:
        logger.error(f"Failed to start macro probe: {e}")

    # Start CryptoQuant on-chain data refresh (4-hour cadence)
    try:
        cryptoquant_task = asyncio.create_task(cryptoquant_supervisor())
        logger.info("CryptoQuant scheduler started")
    except Exception as e:
        logger.error(f"Failed to start CryptoQuant scheduler: {e}")

    yield

    logger.info("Application shutdown")
    for task in [bot_task, crawler_task, etf_scraper_task, coinglass_task, macro_probe_task, cryptoquant_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await engine.dispose()


app = FastAPI(
    title="Axiom OS API",
    description="Backend Engine for Axiom Financial Co-Pilot - Phase 2.0",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json"
)

# CORS Middleware — env var'dan dinamik origin listesi
# ALLOWED_ORIGINS env var virgülle ayrılmış URL listesi olmalı.
# Örn: "https://axiom-dashboard-sigma.vercel.app,http://localhost:3000"
#
# Production-safe default: includes the live Vercel prod domain
# (`axiom-dashboard-sigma.vercel.app`) so the dashboard can call the API
# even if ALLOWED_ORIGINS env is missing/wrong on Railway. Day 21 we hit
# CORS preflight 400 ("Disallowed CORS origin") which silently broke the
# Telegram /login deep-link auth (useAuth.checkAuth → /users/me preflight
# fail → logout → /auth/login). Hardcoding the prod domain here makes the
# system tolerant of env drift between Vercel and Railway.
_default_origins = ",".join([
    "https://axiom-dashboard-sigma.vercel.app",
    "https://axiom-dashboard.vercel.app",
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
])
_allowed_origins = [
    o.strip() for o in os.getenv("ALLOWED_ORIGINS", _default_origins).split(",") if o.strip()
]
logger.info(f"CORS allowed origins: {_allowed_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# Rate limiting — per-IP via slowapi. Limiter is shared with router modules
# through core.rate_limit. SlowAPIMiddleware injects request.state for the
# @limiter.limit decorators to read. Exceeded → HTTP 429 via _rate_limit_exceeded_handler.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Include API routers
app.include_router(v1_router)


@app.get("/")
async def read_root():
    """Root endpoint - API status"""
    return {
        "message": "Axiom OS API is running",
        "version": "2.0.0",
        "status": "operational"
    }


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "axiom-api",
        "version": "2.0.0"
    }


@app.get("/api/v1/status")
async def api_status():
    """API status endpoint"""
    return {
        "api": "v1",
        "status": "operational",
        "endpoints": [
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/users/me",
            "/api/v1/news"
        ]
    }


# Error handlers
@app.exception_handler(Exception)
async def generic_exception_handler(request, exc):
    """Generic exception handler"""
    logger.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "type": "internal_server_error"
        }
    )
