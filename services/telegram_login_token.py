"""
Telegram deep-link login token service.

Bot generates a 5-minute one-time token; user clicks the deep-link;
dashboard exchanges token for JWT via /auth/telegram-token.

Why this design:
- Telegram ID alone is brute-forceable (numeric, leaked in groups)
- The bot is the trusted side; only it can prove the user really
  controls a given telegram_id (because Telegram delivered the link)
- One-time + short TTL prevents replay even if the link is logged
"""
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import text
from core.database import engine
from core.logger import get_logger

logger = get_logger("telegram_login_token")

_TTL = timedelta(minutes=5)


async def create_token(telegram_id: str) -> Optional[str]:
    """Generate a fresh one-time token for the given telegram_id."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + _TTL
    sql = text("""
        INSERT INTO telegram_login_token (token, telegram_id, created_at, expires_at)
        VALUES (:t, :tid, NOW(), :exp)
    """)
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, {"t": token, "tid": str(telegram_id), "exp": expires_at})
        return token
    except Exception as e:
        logger.error(f"create_token error tid={telegram_id}: {e}")
        return None


async def consume_token(token: str) -> Optional[str]:
    """Atomically validate + mark used. Returns telegram_id on success or
    None if token unknown/expired/already-used."""
    sql = text("""
        UPDATE telegram_login_token
        SET used_at = NOW()
        WHERE token = :t
          AND expires_at > NOW()
          AND used_at IS NULL
        RETURNING telegram_id
    """)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, {"t": token})).fetchone()
            if row:
                return row[0]
    except Exception as e:
        logger.warning(f"consume_token error: {e}")
    return None


async def cleanup_expired() -> int:
    """Best-effort sweeper — delete tokens older than 1 day. Optional."""
    sql = text("""
        DELETE FROM telegram_login_token
        WHERE expires_at < NOW() - INTERVAL '1 day'
    """)
    try:
        async with engine.begin() as conn:
            r = await conn.execute(sql)
            return r.rowcount or 0
    except Exception as e:
        logger.warning(f"cleanup_expired error: {e}")
        return 0
