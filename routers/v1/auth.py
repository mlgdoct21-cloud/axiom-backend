"""Authentication routes - Register, Login, Token Refresh"""

import os
import hmac
from fastapi import APIRouter, Depends, HTTPException, Header, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta
from typing import Optional

from core.database import get_db
from core.rate_limit import limiter
from core.security import get_current_user as get_authenticated_user
from schemas.user_schema import UserCreate, UserLogin, UserResponse
from schemas.error_schema import ErrorResponse
from services.user import UserService
from services.auth import AuthService
from services.telegram_login_token import consume_token
from core.logger import get_logger

logger = get_logger("auth_router")

# Bot Internal Secret — sadece Telegram bot bu değere sahip olmalı.
# Bu değer olmadan /login endpoint'i kullanılamaz (telegram_id yetkilendirmesi
# tek başına yetersiz, çünkü Telegram ID'leri tahmin edilebilir sayılardır).
BOT_INTERNAL_SECRET = os.getenv("BOT_INTERNAL_SECRET", "").strip()
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()


def _verify_bot_secret(x_bot_secret: Optional[str]) -> None:
    """Telegram bot dışındaki kaynaklardan /login çağrısı yapılmasını engeller.

    Production'da BOT_INTERNAL_SECRET zorunlu. Geliştirmede setlenmemişse
    sadece uyarı verir (geriye uyumluluk için).

    EXPLICIT BYPASS: ALLOW_PUBLIC_DASHBOARD_LOGIN=true env var dashboard
    kullanıcılarının doğrudan Telegram ID ile login olmasına izin verir.
    Bu geçici bir flag — Day 21+'de Telegram deep-link auth flow'u (bot
    `/login` komutu → tek-kullanımlık token → dashboard /auth/telegram)
    eklendiğinde bu bypass kapatılmalıdır.
    """
    if os.getenv("ALLOW_PUBLIC_DASHBOARD_LOGIN", "").strip().lower() in ("true", "1", "yes"):
        return  # Dashboard login geçici olarak public — Day 21'de kapatılacak

    if not BOT_INTERNAL_SECRET:
        if ENVIRONMENT == "production":
            logger.error("BOT_INTERNAL_SECRET production'da set edilmemiş — /login kapalı")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service misconfiguration",
            )
        logger.warning("⚠️  BOT_INTERNAL_SECRET set değil — geliştirme modu, secret bypass")
        return

    if not x_bot_secret or not hmac.compare_digest(x_bot_secret, BOT_INTERNAL_SECRET):
        logger.warning("Login attempt without valid X-Bot-Secret header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

router = APIRouter(
    prefix="/auth",
    tags=["authentication"],
)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid input or user already exists"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def register(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Register a new user

    - **telegram_id**: User's Telegram ID (1-20 characters)
    - **username**: Optional username (max 32 characters)
    """
    try:
        # Validate telegram_id
        if not user_data.telegram_id or len(user_data.telegram_id) > 20:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid telegram_id"
            )

        # Check if user already exists
        existing_user = await UserService.get_user_by_telegram_id(db, user_data.telegram_id)
        if existing_user:
            logger.warning(f"Registration attempt for existing user: {user_data.telegram_id}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User already exists"
            )

        # Create user
        user = await UserService.create_user(db, user_data)
        logger.info(f"User registered: {user_data.telegram_id}")
        return user
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registration failed"
        )


@router.post(
    "/login",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid credentials"},
        404: {"model": ErrorResponse, "description": "User not found"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
@limiter.limit("10/minute")
async def login(
    request: Request,
    login_data: UserLogin,
    x_bot_secret: Optional[str] = Header(default=None, alias="X-Bot-Secret"),
    db: AsyncSession = Depends(get_db)
):
    """
    Login user and get access token (internal — Telegram bot only)

    - **telegram_id**: User's Telegram ID
    - **X-Bot-Secret** header: BOT_INTERNAL_SECRET değeri (zorunlu, production'da)

    Bu endpoint Telegram bot tarafından, kullanıcı `/start` komutuyla doğrulandıktan
    sonra çağrılır. Doğrudan dış istek yapmak güvenlik politikasına aykırıdır.

    Returns access token and refresh token
    """
    # Bot secret doğrulaması — Telegram ID tek başına auth için yetersiz
    _verify_bot_secret(x_bot_secret)

    try:
        # Get user
        user = await UserService.get_user_by_telegram_id(db, login_data.telegram_id)
        if not user:
            logger.warning(f"Login attempt for non-existent user: {login_data.telegram_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        # Check if user is active
        if not user.is_active:
            logger.warning(f"Login attempt for inactive user: {login_data.telegram_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User account is inactive"
            )

        # Create tokens (sub must be string per JWT spec)
        access_token = AuthService.create_access_token(
            data={"sub": str(user.id), "telegram_id": user.telegram_id}
        )
        refresh_token = AuthService.create_refresh_token(
            data={"sub": str(user.id), "telegram_id": user.telegram_id}
        )

        logger.info(f"User logged in: {login_data.telegram_id}")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user.id,
                "telegram_id": user.telegram_id,
                "username": user.username,
                "tier": (getattr(user, "tier", "free") or "free"),
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Login failed"
        )


@router.post(
    "/refresh",
    response_model=dict,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid or expired token"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
@limiter.limit("30/minute")
async def refresh_token(
    request: Request,
    body: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    Refresh access token using refresh token

    Request body:
    - **refresh_token**: Valid refresh token

    Rate-limited to 30/min per IP — refresh fires routinely on tab focus,
    so the limit must accommodate a few users behind a NAT; a flood beyond
    that suggests a misbehaving client or brute-force probing.
    """
    try:
        refresh_token = body.get("refresh_token")
        if not refresh_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing refresh_token"
            )

        # Verify refresh token
        payload = AuthService.verify_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            logger.warning("Invalid refresh token")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token"
            )

        user_id = payload.get("sub")
        user = await UserService.get_user_by_id(db, user_id)
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive"
            )

        # Create new access token
        new_access_token = AuthService.create_access_token(
            data={"sub": user.id, "telegram_id": user.telegram_id}
        )

        logger.info(f"Token refreshed for user: {user_id}")
        return {
            "access_token": new_access_token,
            "token_type": "bearer"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token refresh failed"
        )


@router.get(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "User not found"}
    }
)
async def get_current_user(
    current_user = Depends(get_authenticated_user),
):
    """
    Get current authenticated user info

    Requires: Authorization header with Bearer token (e.g., `Authorization: Bearer <jwt>`)
    """
    return UserResponse.from_orm(current_user)


# ── Telegram deep-link login (T7) ────────────────────────────────────────────

class TelegramTokenLogin(BaseModel):
    token: str


@router.post(
    "/telegram-token",
    status_code=status.HTTP_200_OK,
    responses={
        401: {"description": "Invalid or expired token"},
        404: {"description": "User not found"},
    },
)
@limiter.limit("10/minute")
async def telegram_token_login(
    request: Request,
    body: TelegramTokenLogin,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a one-time Telegram-issued token for JWT.

    Flow:
    1. User runs `/login` in the bot → bot issues short-lived token
    2. User clicks deep-link → dashboard /auth/telegram?token=<x>
    3. Dashboard POSTs token here → we consume + return JWT pair

    No X-Bot-Secret needed — token itself is the proof of possession.
    Rate-limited to 10/min per IP — guards against token-guessing attempts
    on the 64-char surface area (in practice tokens are single-use and
    expire fast, so brute force is already infeasible; this is belt-and-suspenders).
    """
    if not body.token or len(body.token) > 64:
        raise HTTPException(status_code=400, detail="Invalid token format")

    telegram_id = await consume_token(body.token)
    if not telegram_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token invalid, expired, or already used",
        )

    user = await UserService.get_user_by_telegram_id(db, telegram_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="User account is inactive")

    access_token = AuthService.create_access_token(
        data={"sub": str(user.id), "telegram_id": user.telegram_id}
    )
    refresh_token = AuthService.create_refresh_token(
        data={"sub": str(user.id), "telegram_id": user.telegram_id}
    )

    logger.info(f"Telegram-token login: {telegram_id}")
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "telegram_id": user.telegram_id,
            "username": user.username,
            "tier": (getattr(user, "tier", "free") or "free"),
        },
    }
