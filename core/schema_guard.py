"""
Schema Guard — Idempotent Runtime DDL

Ensures critical columns exist on startup even if alembic migrations haven't run.
This is a safety net for Railway/Docker deployments where manual migration steps
might be skipped. All operations are idempotent (IF NOT EXISTS semantics).

Why this exists:
    - Railway doesn't auto-run `alembic upgrade head` on deploy.
    - create_all() does NOT add columns to existing tables.
    - Missing columns cause crawler crashes that are hard to debug.

This runs once at FastAPI startup via lifespan.
"""
from sqlalchemy import text
from core.database import engine
from core.logger import get_logger

logger = get_logger("schema_guard")


# SQL statements that are safe to run multiple times.
# Each tuple: (description, sql). Use "IF NOT EXISTS" where possible.
# PostgreSQL (Supabase) supports this. SQLite silently ignores unknown syntax — for
# local dev we rely on create_all() handling table creation.
_POSTGRES_GUARDS = [
    # 3-tier columns — PostgreSQL (Supabase) tarafında hiç oluşturulmamış olabilir
    # (SQLite üzerinde create_all ile var, PG üzerinde alembic migration atlanmış).
    (
        "news_items.telegram_hook",
        "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS telegram_hook TEXT",
    ),
    (
        "news_items.dashboard_summary",
        "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS dashboard_summary TEXT",
    ),
    (
        "news_items.axiom_analysis",
        "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS axiom_analysis TEXT",
    ),
    # Two-stage pipeline columns
    (
        "news_items.symbol",
        "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS symbol VARCHAR(32)",
    ),
    (
        "news_items.analyzed",
        "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS analyzed BOOLEAN NOT NULL DEFAULT FALSE",
    ),
    (
        "news_items.broadcast_at",
        "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS broadcast_at TIMESTAMPTZ",
    ),
    (
        "news_items.is_urgent",
        "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS is_urgent BOOLEAN NOT NULL DEFAULT FALSE",
    ),
    (
        "news_items.body",
        "ALTER TABLE news_items ADD COLUMN IF NOT EXISTS body TEXT",
    ),
    (
        "ix_news_items_symbol",
        "CREATE INDEX IF NOT EXISTS ix_news_items_symbol ON news_items(symbol)",
    ),
    (
        "ix_news_items_analyzed",
        "CREATE INDEX IF NOT EXISTS ix_news_items_analyzed ON news_items(analyzed)",
    ),
    (
        "ix_news_items_created_at",
        "CREATE INDEX IF NOT EXISTS ix_news_items_created_at ON news_items(created_at)",
    ),
]


async def ensure_schema() -> None:
    """
    Runs idempotent ALTER TABLE statements to guarantee the pipeline columns exist.

    Called from FastAPI lifespan on startup. Silent on success, logs at WARNING on
    failure (but never raises — we don't want a schema guard failure to kill the app).
    """
    dialect = engine.dialect.name  # 'postgresql', 'sqlite', ...
    if dialect != "postgresql":
        logger.info(f"Schema guard skipped (dialect={dialect}); create_all covers it.")
        return

    try:
        async with engine.begin() as conn:
            for name, sql in _POSTGRES_GUARDS:
                try:
                    await conn.execute(text(sql))
                    logger.debug(f"  ✓ {name}")
                except Exception as e:
                    # Don't halt on a single failure — keep trying the rest.
                    logger.warning(f"  ✗ {name}: {e}")
        logger.info("Schema guard tamamlandı (news_items pipeline kolonları doğrulandı).")
    except Exception as e:
        logger.warning(f"Schema guard genel hata (yok sayılıyor): {e}")
