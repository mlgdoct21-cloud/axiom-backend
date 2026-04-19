import asyncio
import sys
from sqlalchemy.future import select
from core.database import engine, Base, AsyncSessionLocal
from core.logger import get_logger
from services.telegram_bot import start_telegram_bot
from services.crawler import run_crawler
import models

logger = get_logger("bot_runner")

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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Veritabanı yapısı doğrulandı.")

    await migrate_db()
    await seed_sources()

    logger.info("Sistem Ayağa Kalkıyor: Bot ve Crawler aktif ediliyor...")

    try:
        await asyncio.gather(
            start_telegram_bot(),
            run_crawler()
        )
    except Exception as e:
        logger.error(f"Sistem Kritik Hatası: {e}", exc_info=True)

if __name__ == "__main__":
    """
    Self-healing bot runner with automatic recovery from failures.
    Restarts the system every 5 seconds if it crashes.
    """
    while True:
        try:
            logger.info("=" * 60)
            logger.info("🚀 AXIOM OS BAŞLANIYOR (Self-Healing Mode Active)...")
            logger.info("=" * 60)
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("=" * 60)
            logger.info("🛑 Axiom OS Güvenli Şekilde Kapatıldı (User Interrupt).")
            logger.info("=" * 60)
            sys.exit(0)
        except Exception as e:
            logger.error(f"❌ KRITIK HATA: {e}", exc_info=True)
            logger.warning("⏳ 5 saniye içinde yeniden başlanıyor...")
            import time
            time.sleep(5)
