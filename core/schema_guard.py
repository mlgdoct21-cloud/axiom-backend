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
    # alembic 005 — sector chip persistence (Gemini already produces these,
    # this just finally stores them for the broadcaster + dashboard to render).
    (
        "macro_releases.sectors_positive",
        "ALTER TABLE macro_releases ADD COLUMN IF NOT EXISTS sectors_positive JSONB NOT NULL DEFAULT '[]'::jsonb",
    ),
    (
        "macro_releases.sectors_negative",
        "ALTER TABLE macro_releases ADD COLUMN IF NOT EXISTS sectors_negative JSONB NOT NULL DEFAULT '[]'::jsonb",
    ),
    # alembic 006 — admin-entered consensus % readings (MoM + YoY).
    (
        "macro_releases.expected_mom_pct",
        "ALTER TABLE macro_releases ADD COLUMN IF NOT EXISTS expected_mom_pct NUMERIC(8,4)",
    ),
    (
        "macro_releases.expected_yoy_pct",
        "ALTER TABLE macro_releases ADD COLUMN IF NOT EXISTS expected_yoy_pct NUMERIC(8,4)",
    ),
    # Macro source reliability — append-only probe log. Hafta 1 verification
    # criterion (>=99% uptime, p95<3s) is computed by rolling SELECT over the
    # last N hours of rows. ~2000 rows/source/week at 5-min cadence.
    (
        "macro_source_health table",
        """CREATE TABLE IF NOT EXISTS macro_source_health (
            id BIGSERIAL PRIMARY KEY,
            source VARCHAR(32) NOT NULL,
            probed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            success BOOLEAN NOT NULL,
            latency_ms INTEGER,
            http_status INTEGER,
            not_modified BOOLEAN NOT NULL DEFAULT FALSE,
            payload_bytes INTEGER,
            events_extracted INTEGER,
            error_msg TEXT
        )""",
    ),
    (
        "ix_macro_source_health_source_probed",
        "CREATE INDEX IF NOT EXISTS ix_macro_source_health_source_probed ON macro_source_health(source, probed_at DESC)",
    ),
    (
        "ix_macro_source_health_probed",
        "CREATE INDEX IF NOT EXISTS ix_macro_source_health_probed ON macro_source_health(probed_at DESC)",
    ),
    # Append-only market-pricing snapshots (Kalshi KXFED, etc). Narrative reads
    # the last row before / first row after a release_at to compute the
    # before/after delta that drives the "Piyasa önce X, ŞİMDİ Y" line.
    (
        "macro_market_pricing table",
        """CREATE TABLE IF NOT EXISTS macro_market_pricing (
            id BIGSERIAL PRIMARY KEY,
            source VARCHAR(32) NOT NULL,
            meeting_ticker VARCHAR(32) NOT NULL,
            snapshot_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            modal_rate_pct NUMERIC(6, 3),
            modal_prob NUMERIC(5, 4),
            distribution JSONB,
            payload_bytes INTEGER
        )""",
    ),
    (
        "ix_macro_market_pricing_meeting_ts",
        "CREATE INDEX IF NOT EXISTS ix_macro_market_pricing_meeting_ts ON macro_market_pricing(meeting_ticker, snapshot_ts DESC)",
    ),
    # alembic 007 — market reaction snapshots (DXY/SPY/US10Y T+0 + T+5min).
    (
        "macro_release_market_snapshots table",
        """CREATE TABLE IF NOT EXISTS macro_release_market_snapshots (
            id BIGSERIAL PRIMARY KEY,
            event_id VARCHAR(64) NOT NULL,
            t_offset_seconds INTEGER NOT NULL,
            dxy NUMERIC(10,4),
            spy NUMERIC(12,4),
            us10y NUMERIC(8,4),
            taken_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    ),
    (
        "ix_market_snapshots_event_offset",
        "CREATE INDEX IF NOT EXISTS ix_market_snapshots_event_offset ON macro_release_market_snapshots(event_id, t_offset_seconds)",
    ),
    # alembic 008 — user subscription tier (free / premium / advance).
    (
        "users.tier",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS tier VARCHAR(20) NOT NULL DEFAULT 'free'",
    ),
    # alembic 009 — macro release broadcast timestamp. /macro/latest needs to
    # sort by this so a freshly-broadcast NFP outranks a PCE row whose
    # narrative was regenerated on probe but whose Telegram fan-out is older.
    (
        "macro_releases.last_broadcast_at",
        "ALTER TABLE macro_releases ADD COLUMN IF NOT EXISTS last_broadcast_at TIMESTAMPTZ",
    ),
    (
        "macro_releases.last_broadcast_at backfill",
        "UPDATE macro_releases SET last_broadcast_at = created_at WHERE last_broadcast_at IS NULL",
    ),
    # alembic 010 — Stripe billing handles on users.
    (
        "users.stripe_customer_id",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(64)",
    ),
    (
        "users.stripe_subscription_id",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(64)",
    ),
    (
        "users.subscription_status",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(32)",
    ),
    # alembic 011 — CryptoQuant on-chain metric cache.
    # NOTE: "window" is a PostgreSQL reserved keyword (window functions);
    # column name + index references must be double-quoted.
    (
        "cryptoquant_cache table",
        """CREATE TABLE IF NOT EXISTS cryptoquant_cache (
            id BIGSERIAL PRIMARY KEY,
            metric_key VARCHAR(64) NOT NULL,
            symbol VARCHAR(16) NOT NULL,
            "window" VARCHAR(16) NOT NULL,
            data JSONB NOT NULL,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL
        )""",
    ),
    (
        "uq_cryptoquant_cache_key_sym_win",
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_cryptoquant_cache_key_sym_win ON cryptoquant_cache(metric_key, symbol, "window")',
    ),
    (
        "ix_cryptoquant_cache_expires",
        "CREATE INDEX IF NOT EXISTS ix_cryptoquant_cache_expires ON cryptoquant_cache(expires_at)",
    ),
    # alembic 012 — alert log + cooldown persistence (multi-replica safe)
    (
        "cryptoquant_alert_log table",
        """CREATE TABLE IF NOT EXISTS cryptoquant_alert_log (
            id BIGSERIAL PRIMARY KEY,
            telegram_id VARCHAR(32) NOT NULL,
            alert_key VARCHAR(64) NOT NULL,
            severity VARCHAR(16),
            title VARCHAR(128),
            sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sent_date DATE NOT NULL
        )""",
    ),
    (
        "ix_cq_alert_log_user_date",
        "CREATE INDEX IF NOT EXISTS ix_cq_alert_log_user_date ON cryptoquant_alert_log(telegram_id, sent_date)",
    ),
    (
        "ix_cq_alert_log_sent_at",
        "CREATE INDEX IF NOT EXISTS ix_cq_alert_log_sent_at ON cryptoquant_alert_log(sent_at)",
    ),
    (
        "cryptoquant_alert_cooldown table",
        """CREATE TABLE IF NOT EXISTS cryptoquant_alert_cooldown (
            alert_key VARCHAR(64) PRIMARY KEY,
            expires_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    ),
    (
        "ix_cq_alert_cooldown_expires",
        "CREATE INDEX IF NOT EXISTS ix_cq_alert_cooldown_expires ON cryptoquant_alert_cooldown(expires_at)",
    ),
    # alembic 013 — Axiom Skor günlük tarihçe (T5: dün vs bugün karşılaştırma)
    (
        "axiom_score_history table",
        """CREATE TABLE IF NOT EXISTS axiom_score_history (
            id BIGSERIAL PRIMARY KEY,
            symbol VARCHAR(16) NOT NULL,
            score NUMERIC(5,2) NOT NULL,
            score_zone VARCHAR(16) NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            recorded_date DATE NOT NULL
        )""",
    ),
    (
        "uq_axiom_score_history_sym_date",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_axiom_score_history_sym_date ON axiom_score_history(symbol, recorded_date)",
    ),
    (
        "ix_axiom_score_history_recorded_at",
        "CREATE INDEX IF NOT EXISTS ix_axiom_score_history_recorded_at ON axiom_score_history(recorded_at)",
    ),
    # alembic 014 — Telegram deep-link login token (T7)
    (
        "telegram_login_token table",
        """CREATE TABLE IF NOT EXISTS telegram_login_token (
            token VARCHAR(64) PRIMARY KEY,
            telegram_id VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,
            used_at TIMESTAMPTZ
        )""",
    ),
    (
        "ix_telegram_login_token_expires",
        "CREATE INDEX IF NOT EXISTS ix_telegram_login_token_expires ON telegram_login_token(expires_at)",
    ),
    (
        "ix_telegram_login_token_telegram_id",
        "CREATE INDEX IF NOT EXISTS ix_telegram_login_token_telegram_id ON telegram_login_token(telegram_id)",
    ),
    # alembic 015 — Dashboard feature quota (sliding 24h)
    (
        "feature_quota_log table",
        """CREATE TABLE IF NOT EXISTS feature_quota_log (
            id BIGSERIAL PRIMARY KEY,
            telegram_id VARCHAR(32) NOT NULL,
            command VARCHAR(64) NOT NULL,
            used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    ),
    (
        "ix_feature_quota_log_user_cmd_used",
        "CREATE INDEX IF NOT EXISTS ix_feature_quota_log_user_cmd_used ON feature_quota_log(telegram_id, command, used_at)",
    ),
    (
        "ix_feature_quota_log_used_at",
        "CREATE INDEX IF NOT EXISTS ix_feature_quota_log_used_at ON feature_quota_log(used_at)",
    ),
    (
        "stripe_webhook_events table",
        """CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    ),
    (
        "ix_stripe_webhook_events_processed_at",
        "CREATE INDEX IF NOT EXISTS ix_stripe_webhook_events_processed_at ON stripe_webhook_events(processed_at)",
    ),
    # Kurumsal Sentez Faz 1 — alembic 025 ile aynı şema (Railway alembic
    # auto-run yok; bu runtime garanti).
    (
        "corporate_syntheses table",
        """CREATE TABLE IF NOT EXISTS corporate_syntheses (
            id BIGSERIAL PRIMARY KEY,
            event_id TEXT NOT NULL,
            tier TEXT NOT NULL,
            week_start DATE NOT NULL,
            synthesis_md TEXT,
            source_count INT NOT NULL DEFAULT 0,
            meta JSONB,
            generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            broadcasted_premium_at TIMESTAMPTZ,
            broadcasted_advance_at TIMESTAMPTZ,
            CONSTRAINT uq_corp_synth_eid_tier UNIQUE (event_id, tier)
        )""",
    ),
    (
        "ix_corp_synth_week",
        "CREATE INDEX IF NOT EXISTS ix_corp_synth_week ON corporate_syntheses(week_start DESC)",
    ),
    (
        "ix_corp_synth_eid",
        "CREATE INDEX IF NOT EXISTS ix_corp_synth_eid ON corporate_syntheses(event_id)",
    ),
    (
        "corporate_source_state table",
        """CREATE TABLE IF NOT EXISTS corporate_source_state (
            source TEXT PRIMARY KEY,
            etag TEXT,
            last_modified TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
    ),
    # Kurumsal Sentez S1c — accumulation store (alembic 026 ile aynı şema).
    (
        "corporate_posts table",
        """CREATE TABLE IF NOT EXISTS corporate_posts (
            id BIGSERIAL PRIMARY KEY,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            kind TEXT,
            title TEXT NOT NULL,
            link TEXT,
            published TIMESTAMPTZ NOT NULL,
            body_text TEXT,
            truncated BOOLEAN NOT NULL DEFAULT FALSE,
            author TEXT,
            meta JSONB,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_corp_posts_src_eid UNIQUE (source, external_id)
        )""",
    ),
    (
        "ix_corp_posts_src_pub",
        "CREATE INDEX IF NOT EXISTS ix_corp_posts_src_pub ON corporate_posts(source, published DESC)",
    ),
    (
        "ix_corp_posts_pub",
        "CREATE INDEX IF NOT EXISTS ix_corp_posts_pub ON corporate_posts(published DESC)",
    ),
    (
        "corporate_holdings_snapshots table",
        """CREATE TABLE IF NOT EXISTS corporate_holdings_snapshots (
            id BIGSERIAL PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'ark',
            fund TEXT NOT NULL,
            as_of DATE NOT NULL,
            payload JSONB NOT NULL,
            holding_count INT NOT NULL DEFAULT 0,
            fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_corp_hold_fund_asof UNIQUE (fund, as_of)
        )""",
    ),
    (
        "ix_corp_hold_fund_asof",
        "CREATE INDEX IF NOT EXISTS ix_corp_hold_fund_asof ON corporate_holdings_snapshots(fund, as_of DESC)",
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
