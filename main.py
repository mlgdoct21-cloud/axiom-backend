from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import logging

from core.database import engine
import models  # Import models so SQLAlchemy knows about them
from routers.v1 import router as v1_router
from services.telegram_bot import start_telegram_bot
from core.logger import get_logger

logger = get_logger("main")

# Reference to bot task
bot_task = None


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

@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_task
    logger.info("Application startup")

    # Note: Database tables should be created by Alembic migrations
    # Not here - this ensures production consistency

    # Start Telegram bot in background within a supervisor
    try:
        bot_task = asyncio.create_task(bot_supervisor())
        logger.info("Telegram bot supervisor started")
    except Exception as e:
        logger.error(f"Failed to start bot supervisor: {e}")

    yield

    logger.info("Application shutdown")
    # Cancel bot task on shutdown
    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            logger.info("Bot task cancelled")

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

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:3000", "http://127.0.0.1:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
