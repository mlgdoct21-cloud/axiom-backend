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
    # ETF flow cache (multi-source scraper backing store) — günlük scrape sonuçları
    (
        "etf_flow_cache table",
        """CREATE TABLE IF NOT EXISTS etf_flow_cache (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(8) NOT NULL,
            net_flow_usd NUMERIC(20, 2) NOT NULL,
            net_flow_coins NUMERIC(20, 4) NOT NULL,
            spot_price NUMERIC(20, 2),
            total_aum_usd NUMERIC(20, 2),
            total_holdings_coins NUMERIC(20, 4),
            source VARCHAR(32) NOT NULL,
            raw_data JSONB,
            scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    ),
    (
        "idx_etf_symbol_scraped",
        "CREATE INDEX IF NOT EXISTS idx_etf_symbol_scraped ON etf_flow_cache(symbol, scraped_at DESC)",
    ),
    (
        "idx_etf_scraped_at",
        "CREATE INDEX IF NOT EXISTS idx_etf_scraped_at ON etf_flow_cache(scraped_at DESC)",
    ),
    # Macro Intelligence Layer (alembic 004) — runtime guard so Railway picks it
    # up without manual `alembic upgrade head`. Numeric (not float) so the
    # narrative number-validator can do exact-string regex matches.
    (
        "macro_releases table",
        """CREATE TABLE IF NOT EXISTS macro_releases (
            event_id VARCHAR(64) PRIMARY KEY,
            event_type VARCHAR(32) NOT NULL,
            country VARCHAR(8) NOT NULL DEFAULT 'US',
            scheduled_at TIMESTAMPTZ,
            released_at TIMESTAMPTZ,
            prior_value NUMERIC(18, 6),
            expected_value NUMERIC(18, 6),
            actual_value NUMERIC(18, 6),
            surprise_pct NUMERIC(10, 4),
            source VARCHAR(32),
            source_url TEXT,
            narrative_md TEXT,
            sentiment_score NUMERIC(4, 3),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    ),
    (
        "ix_macro_releases_scheduled_at",
        "CREATE INDEX IF NOT EXISTS ix_macro_releases_scheduled_at ON macro_releases(scheduled_at)",
    ),
    (
        "ix_macro_releases_event_type_released_at",
        "CREATE INDEX IF NOT EXISTS ix_macro_releases_event_type_released_at ON macro_releases(event_type, released_at)",
    ),
    (
        "ix_macro_releases_country",
        "CREATE INDEX IF NOT EXISTS ix_macro_releases_country ON macro_releases(country)",
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
