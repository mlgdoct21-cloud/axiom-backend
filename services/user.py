"""User service - CRUD operations for user management"""

from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models.user import User
from schemas.user_schema import UserCreate, UserUpdate, UserResponse
from services.auth import AuthService
from core.logger import get_logger

logger = get_logger("user_service")


class UserService:
    """User management service with CRUD operations"""

    @staticmethod
    async def create_user(db: AsyncSession, user_data: UserCreate) -> UserResponse:
        """Create a new user"""
        try:
            # Check if user already exists
            existing = await UserService.get_user_by_telegram_id(db, user_data.telegram_id)
            if existing:
                logger.warning(f"User with telegram_id {user_data.telegram_id} already exists")
                return UserResponse.from_orm(existing)

            # Create new user
            db_user = User(
                telegram_id=user_data.telegram_id,
                username=user_data.username,
                is_active=True,
                tags="",
                report_mode="digest",
                report_hours="08:00",
                custom_follows=""
            )
            db.add(db_user)
            await db.commit()
            await db.refresh(db_user)
            logger.info(f"User created: {user_data.telegram_id}")
            return UserResponse.from_orm(db_user)
        except Exception as e:
            logger.error(f"Error creating user: {e}")
            await db.rollback()
            raise

    @staticmethod
    async def get_user_by_id(db: AsyncSession, user_id: int) -> Optional[User]:
        """Get user by ID"""
        try:
            result = await db.execute(select(User).filter(User.id == user_id))
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting user by ID {user_id}: {e}")
            return None

    @staticmethod
    async def get_user_by_telegram_id(db: AsyncSession, telegram_id: str) -> Optional[User]:
        """Get user by Telegram ID"""
        try:
            result = await db.execute(
                select(User).filter(User.telegram_id == telegram_id)
            )
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting user by telegram_id {telegram_id}: {e}")
            return None

    @staticmethod
    async def get_all_users(db: AsyncSession, skip: int = 0, limit: int = 100) -> List[User]:
        """Get all users with pagination"""
        try:
            result = await db.execute(
                select(User).offset(skip).limit(limit)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting all users: {e}")
            return []

    @staticmethod
    async def get_active_users(db: AsyncSession) -> List[User]:
        """Get all active users (for broadcasting notifications)"""
        try:
            result = await db.execute(
                select(User).filter(User.is_active == True)
            )
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting active users: {e}")
            return []

    @staticmethod
    async def update_user(db: AsyncSession, user_id: int, user_data: UserUpdate) -> Optional[UserResponse]:
        """Update user settings"""
        try:
            db_user = await UserService.get_user_by_id(db, user_id)
            if not db_user:
                logger.warning(f"User {user_id} not found")
                return None

            # Update fields
            update_data = user_data.dict(exclude_unset=True)
            for field, value in update_data.items():
                setattr(db_user, field, value)

            db.add(db_user)
            await db.commit()
            await db.refresh(db_user)
            logger.info(f"User {user_id} updated")
            return UserResponse.from_orm(db_user)
        except Exception as e:
            logger.error(f"Error updating user {user_id}: {e}")
            await db.rollback()
            raise

    @staticmethod
    async def update_tags(db: AsyncSession, user_id: int, tags: str) -> Optional[UserResponse]:
        """Update user tags"""
        try:
            db_user = await UserService.get_user_by_id(db, user_id)
            if not db_user:
                return None

            db_user.tags = tags
            db.add(db_user)
            await db.commit()
            await db.refresh(db_user)
            logger.info(f"User {user_id} tags updated: {tags}")
            return UserResponse.from_orm(db_user)
        except Exception as e:
            logger.error(f"Error updating tags for user {user_id}: {e}")
            await db.rollback()
            raise

    @staticmethod
    async def deactivate_user(db: AsyncSession, user_id: int) -> bool:
        """Deactivate user (soft delete)"""
        try:
            db_user = await UserService.get_user_by_id(db, user_id)
            if not db_user:
                return False

            db_user.is_active = False
            db.add(db_user)
            await db.commit()
            logger.info(f"User {user_id} deactivated")
            return True
        except Exception as e:
            logger.error(f"Error deactivating user {user_id}: {e}")
            await db.rollback()
            return False

    @staticmethod
    async def delete_user(db: AsyncSession, user_id: int) -> bool:
        """Permanently delete user"""
        try:
            db_user = await UserService.get_user_by_id(db, user_id)
            if not db_user:
                return False

            await db.delete(db_user)
            await db.commit()
            logger.info(f"User {user_id} permanently deleted")
            return True
        except Exception as e:
            logger.error(f"Error deleting user {user_id}: {e}")
            await db.rollback()
            return False
