"""Authentication routes - Register, Login, Token Refresh"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta

from core.database import get_db
from schemas.user_schema import UserCreate, UserLogin, UserResponse
from schemas.error_schema import ErrorResponse
from services.user import UserService
from services.auth import AuthService
from core.logger import get_logger

logger = get_logger("auth_router")

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
async def login(
    login_data: UserLogin,
    db: AsyncSession = Depends(get_db)
):
    """
    Login user and get access token

    - **telegram_id**: User's Telegram ID

    Returns access token and refresh token
    """
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
                "username": user.username
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
async def refresh_token(
    request: dict,
    db: AsyncSession = Depends(get_db)
):
    """
    Refresh access token using refresh token

    Request body:
    - **refresh_token**: Valid refresh token
    """
    try:
        refresh_token = request.get("refresh_token")
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
    token: str = None,
    db: AsyncSession = Depends(get_db)
):
    """
    Get current authenticated user info

    Requires: Authorization header with Bearer token
    """
    try:
        if not token:
            # Try to get from header (in real implementation, use Depends on a security function)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authorization token"
            )

        # Verify token
        payload = AuthService.verify_token(token)
        if not payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )

        user_id = payload.get("sub")
        user = await UserService.get_user_by_id(db, user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )

        return UserResponse.from_orm(user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get current user error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get user info"
        )
