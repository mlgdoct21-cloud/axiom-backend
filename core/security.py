"""Security utilities - Token extraction and validation"""

import hmac
import os
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from core.database import get_db
from services.auth import AuthService
from core.logger import get_logger

logger = get_logger("security")
_admin_auth_logger = get_logger("admin_auth")
_ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()

security = HTTPBearer()


def assert_internal_secret(secret: Optional[str]) -> None:
    """Constant-time check of an admin/internal secret header against
    BOT_INTERNAL_SECRET. Centralized so every admin endpoint uses the same
    timing-attack-resistant comparison (replaces plain `!=` checks that
    used to live duplicated across billing/macro_admin/etf_admin/crypto_onchain).

    Raises HTTPException(403) on missing/mismatched secret. In production
    raises 503 if BOT_INTERNAL_SECRET itself is not configured — a misdeploy
    must NOT accidentally open admin endpoints to anonymous callers.

    Unlike auth.py's `_verify_bot_secret` (which has the explicit
    ALLOW_PUBLIC_DASHBOARD_LOGIN bypass for the user-login path), admin
    endpoints never allow bypass: they trigger destructive ops (force probes,
    tier edits, manual broadcasts) so the secret is always required.
    """
    expected = os.getenv("BOT_INTERNAL_SECRET", "").strip()

    if not expected:
        if _ENVIRONMENT == "production":
            _admin_auth_logger.error(
                "BOT_INTERNAL_SECRET not set in production — admin endpoints disabled"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service misconfiguration",
            )
        # Dev mode: still deny. Admin endpoints don't auto-bypass — set
        # BOT_INTERNAL_SECRET to anything (even a placeholder) to test
        # admin paths locally. Auto-bypass here would be too dangerous if
        # an admin route was accidentally exposed during dev.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden",
        )

    if not secret or not hmac.compare_digest(secret, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden",
        )


async def get_current_user_id(
    credentials = Depends(security),
) -> int:
    """
    Extract and validate user ID from Bearer token

    Returns the user_id from the token payload
    """
    token = credentials.credentials

    # Verify token
    payload = AuthService.verify_token(token)
    if not payload:
        logger.warning("Invalid token attempted")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: missing user ID",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Convert string user_id back to int
    try:
        return int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: invalid user ID format",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    user_id: int = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db)
):
    """
    Get current authenticated user from database

    Returns the User object
    """
    from services.user import UserService

    user = await UserService.get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    return user
