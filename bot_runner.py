import asyncio
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from sqlalchemy.future import select
from core.database import engine, Base, AsyncSessionLocal
from core.logger import get_logger
from services.telegram_bot import start_telegram_bot
from services.crawler import run_crawler
import models

logger = get_logger("bot_runner")

# Simple HTTP health check for Railway
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logging

DEFAULT_SOURCES = [
    {"name": "Investing.com",        "url": "https://www.investing.com/rss/news.rss"},
    {"name": "Yahoo Finance (AAPL)", "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US"},
    {"name": "Bloomberg HT",         "url": "https://www.bloomberght.com/rss"},
]

async def migrate_db():
    """Mevcut tablolara eksik kolon ve index'leri ekler (Alembic olmadan basit migration)."""
    async with engine.begin() as conn:
        # custom_follows kolonu
        try:
            await conn.execute(__import__('sqlalchemy').text(
                "ALTER TABLE users ADD COLUMN custom_follows VARCHAR DEFAULT ''"
            ))
            logger.info("Migration: users.custom_follows kolonu eklendi.")
        except Exception:
            pass

        # news_items.original_link için unique index — duplicate race condition'ı önler
        try:
            await conn.execute(__import__('sqlalchemy').text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_news_items_original_link ON news_items (original_link)"
            ))
            logger.info("Migration: news_items.original_link unique index oluşturuldu.")
        except Exception:
            pass

        # news_items.body kolonu (v3.1 two-stage pipeline için)
        for col_def in [
            ("body", "TEXT"),
            ("symbol", "VARCHAR(32)"),
            ("analyzed", "BOOLEAN DEFAULT 0"),
            ("broadcast_at", "DATETIME"),
            ("is_urgent", "BOOLEAN DEFAULT 0"),
            ("telegram_hook", "TEXT"),
            ("dashboard_summary", "TEXT"),
            ("axiom_analysis", "TEXT"),
        ]:
            col_name, col_type = col_def
            try:
                await conn.execute(__import__('sqlalchemy').text(
                    f"ALTER TABLE news_items ADD COLUMN {col_name} {col_type}"
                ))
                logger.info(f"Migration: news_items.{col_name} kolonu eklendi.")
            except Exception:
                pass  # Kolon zaten var

async def seed_sources():
    """Sources tablosuna varsayılan kaynakları ekler (zaten varsa atlar)."""
    from models.source import Source
    async with AsyncSessionLocal() as session:
        for src in DEFAULT_SOURCES:
            result = await session.execute(select(Source).where(Source.url == src["url"]))
            if not result.scalars().first():
                session.add(Source(name=src["name"], url=src["url"]))
        await session.commit()
    logger.info("Kaynaklar veritabanına yüklendi.")

async def main():
    logger.info("Axiom Financial OS Hazırlanıyor...")

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Veritabanı yapısı doğrulandı.")
    except Exception as e:
        logger.warning(f"⚠️  PostgreSQL bağlantısı başarısız: {e}")
        logger.info("📦 SQLite'a geçiliyor...")
        # Will use SQLite fallback from connection pool

    await migrate_db()
    await seed_sources()

    logger.info("Sistem Ayağa Kalkıyor: Bot ve Crawler aktif ediliyor...")

    async def _supervised_bot():
        """Bot'u izler — exception olursa log'la ve restart et."""
        while True:
            try:
                await start_telegram_bot()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Telegram bot çöktü, 5sn sonra restart: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _supervised_crawler():
        """Crawler'ı izler — exception olursa log'la ve restart et."""
        while True:
            try:
                await run_crawler()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Crawler çöktü, 30sn sonra restart: {e}", exc_info=True)
                await asyncio.sleep(30)

    # return_exceptions=True → bir task çökse bile diğeri yaşar
    results = await asyncio.gather(
        _supervised_bot(),
        _supervised_crawler(),
        return_exceptions=True
    )
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Task kritik hatası: {r}")

if __name__ == "__main__":
    """
    Bot runner with health check server for Railway
    """
    print("STARTING BOT...")  # Direct print to stdout

    # Start HTTP health check server in background thread (for Railway)
    health_server = HTTPServer(("0.0.0.0", 8000), HealthCheckHandler)
    health_thread = threading.Thread(target=health_server.serve_forever, daemon=True)
    health_thread.start()
    logger.info("✅ Health check server started on port 8000")

    logger.info("=" * 60)
    logger.info("🚀 AXIOM OS BAŞLANIYOR...")
    logger.info("=" * 60)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("=" * 60)
        logger.info("🛑 Axiom OS Kapatıldı.")
        logger.info("=" * 60)
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ HATA: {e}", exc_info=True)
        sys.exit(1)
