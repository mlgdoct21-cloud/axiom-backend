"""Authentication service - JWT tokens, password hashing"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from core.logger import get_logger

logger = get_logger("auth")

# Configuration — fail-fast: SECRET_KEY mutlaka env'den gelmek zorunda.
# Production'da zayıf bir fallback ile çalışmak güvenlik açığıdır (JWT forgery riski).
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()

if not SECRET_KEY or len(SECRET_KEY) < 32:
    if ENVIRONMENT == "production":
        raise RuntimeError(
            "SECRET_KEY env var eksik veya çok kısa (min 32 karakter). "
            "Production ortamında bu değer zorunlu. "
            "Üretmek için: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    # Development fallback — sadece local geliştirme için, asla production'da değil
    logger.warning(
        "⚠️  SECRET_KEY env var set edilmemiş — geliştirme fallback'i kullanılıyor. "
        "Production'da bu çağrı RuntimeError fırlatır."
    )
    SECRET_KEY = "dev-only-fallback-not-for-production-please-set-SECRET_KEY-env"

ALGORITHM = "HS256"
# 15 dk çok kısa → kullanıcı her 15 dk'da bir tekrar /login akışına düşüyordu
# (frontend 401 handler yok). 24h'a çıkardık; refresh token (7 gün) hâlâ daha
# uzun kalıyor, leak blast radius makul.
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 saat
REFRESH_TOKEN_EXPIRE_DAYS = 7

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    """JWT and password management"""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password using bcrypt"""
        return pwd_context.hash(password)

    @staticmethod
    def verify_password(plain_password: str, hashed_password: str) -> bool:
        """Verify plain password against hash"""
        return pwd_context.verify(plain_password, hashed_password)

    @staticmethod
    def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
        """Create JWT access token"""
        to_encode = data.copy()

        if expires_delta:
            expire = datetime.now(timezone.utc) + expires_delta
        else:
            expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)

        to_encode.update({"exp": expire})

        try:
            encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
            logger.debug(f"Access token created for user {data.get('sub')}")
            return encoded_jwt
        except Exception as e:
            logger.error(f"Error creating access token: {e}")
            raise

    @staticmethod
    def create_refresh_token(data: dict) -> str:
        """Create JWT refresh token"""
        to_encode = data.copy()
        expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
        to_encode.update({"exp": expire, "type": "refresh"})

        try:
            encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
            logger.debug(f"Refresh token created for user {data.get('sub')}")
            return encoded_jwt
        except Exception as e:
            logger.error(f"Error creating refresh token: {e}")
            raise

    @staticmethod
    def verify_token(token: str) -> Optional[dict]:
        """Verify JWT token and extract payload"""
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            return payload
        except JWTError as e:
            logger.warning(f"Token verification failed: {e}")
            return None

    @staticmethod
    def get_user_id_from_token(token: str) -> Optional[int]:
        """Extract user ID from token"""
        payload = AuthService.verify_token(token)
        if payload:
            return payload.get("sub")
        return None
