import asyncio
import os
import html
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
from core.database import AsyncSessionLocal
from core.logger import get_logger
from models.news import NewsItem
from models.user import User
from models.source import Source
from services.rss_service import fetch_all_feeds, DEFAULT_RSS_FEEDS
from services.ai_service import generate_summary
from services.telegram_bot import send_telegram_message

logger = get_logger("crawler")

# Her tag için eşleşme anahtar kelimeleri (başlık içinde aranır)
TAG_KEYWORDS = {
    "BTC":    ["btc", "bitcoin"],
    "Altın":  ["altın", "altin", "gold", "xau"],
    "BIST":   ["bist", "borsa istanbul", "thyao", "garan", "akbnk", "eregl"],
    "Dolar":  ["dolar", "dollar", "usd"],
    "Faiz":   ["faiz", "interest rate", "fed funds", "tcmb"],
    "Fed":    ["fed", "fomc", "powell", "federal reserve"],
    "Euro":   ["euro", "eur"],
    "Petrol": ["petrol", "oil", "brent", "wti", "opec"],
    "Kripto": ["kripto", "crypto", "ethereum", "eth", "solana"],
    "Hisse":  ["hisse", "stock", "s&p", "nasdaq", "dow jones"],
}

def haber_kullaniciya_uygun(title: str, user_tags: str, custom_follows: str = "") -> bool:
    """Haber başlığı kullanıcının tag veya takip listesiyle eşleşiyor mu?"""
    has_tags = bool(user_tags and user_tags.strip())
    has_follows = bool(custom_follows and custom_follows.strip())

    # İkisi de boşsa her haberi gönder
    if not has_tags and not has_follows:
        return True

    title_lower = title.lower()

    # Predefined tag kontrolü
    if has_tags:
        for tag in [t.strip() for t in user_tags.split(",") if t.strip()]:
            keywords = TAG_KEYWORDS.get(tag, [tag.lower()])
            if any(kw in title_lower for kw in keywords):
                return True

    # Custom takip kelimesi kontrolü
    if has_follows:
        for keyword in [k.strip() for k in custom_follows.split(",") if k.strip()]:
            if keyword.lower() in title_lower:
                return True

    return False

async def get_sources_from_db() -> dict:
    """Aktif kaynakları DB'den çeker. Boşsa varsayılan listeyi döner."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Source).where(Source.is_active == True))
        sources = result.scalars().all()
    if sources:
        return {s.name: s.url for s in sources}
    logger.info("DB'de kaynak bulunamadı, varsayılan kaynaklar kullanılıyor.")
    return DEFAULT_RSS_FEEDS

async def rss_cek():
    """1. Adım: DB'deki aktif kaynaklardan haberleri toplar."""
    logger.info("Kaynaklar taranıyor...")
    sources = await get_sources_from_db()
    return await fetch_all_feeds(sources)

async def duplicate_filtrele(link: str) -> bool:
    """2. Adım: Haberin linki veritabanında var mı? Her çağrıda fresh session kullanır."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(NewsItem).where(NewsItem.original_link == link))
        return result.scalars().first() is None

async def gemini_gonder(title, link):
    """3. Adım: Sadece yeni haberlerin AI analizine gönderilmesi."""
    logger.debug(f"Yeni haber analiz ediliyor: {title[:50]}...")
    return await generate_summary(title, link)

async def telegram_gonder(title, summary, source, link):
    """4. Adım: Özetlerin tag'e uyan kullanıcılara iletilmesi."""
    # Guard against None values from failed API calls
    summary = summary or "⚠️ Özet alınamadı"
    source = source or "Bilinmeyen Kaynak"

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()

    logger.info(f"📤 BROADCAST BAŞLANIYOR: '{title[:50]}...' ({len(users)} user)")

    # Determine emoji based on title content (urgent vs normal)
    emoji = "⚡" if any(word in title.lower() for word in ["dump", "çöküş", "crash", "sert", "hızlı"]) else "📊"
    emoji = "⚠️" if any(word in title.lower() for word in ["risk", "uyarı", "dikkat", "tehdit"]) else emoji
    emoji = "🚀" if any(word in title.lower() for word in ["yüksel", "rally", "pump", "artış", "kazanç"]) else emoji

    safe_link = html.escape(link, quote=True)
    message = (
        f"{emoji} <b>{html.escape(title)}</b>\n\n"
        f"{summary}\n\n"
        f"🔗 <a href='{safe_link}'>Detaylı Analiz →</a> • <b>{html.escape(source)}</b>"
    )

    broadcast_count = 0
    for user in users:
        if haber_kullaniciya_uygun(title, user.tags, user.custom_follows or ""):
            broadcast_count += 1
            logger.info(f"  ✉️ Gönderiliyor → User {user.telegram_id}")
            try:
                send_telegram_message(user.telegram_id, message)
            except Exception as e:
                # Suppress "chat not found" errors - expected when no users registered
                if "chat not found" not in str(e).lower():
                    logger.warning(f"{user.telegram_id} kullanıcısına mesaj gönderilemedi: {e}")

    logger.info(f"✅ BROADCAST TAMAMLANDI: {broadcast_count} mesaj gönderildi")

async def run_crawler():
    """Ana Döngü: Çek -> Filtrele -> Analiz -> İlet -> Bekle"""
    logger.info("Axiom 7/24 Crawler Motoru Başlatıldı.")

    while True:
        try:
            haberler = await rss_cek()

            # Aynı döngüde birden fazla kaynak aynı URL/Title'ı getirirse tekrarı önler
            seen_in_batch = set()

            for i, haber in enumerate(haberler):
                # Guard against None values from RSS
                link = haber.get('link') or '#'
                haber_title = haber.get('title') or 'Başlık Yok'
                title = haber_title.lower().strip()

                # 1. Aynı batch içinde daha önce işlendi mi? (link veya title ile)
                batch_key = (link, title)
                if batch_key in seen_in_batch:
                    logger.debug(f"⏭️  BATCH DEDUP: '{haber_title[:40]}...' zaten batch'te işlendi")
                    continue

                # 2. DB'de daha önce işlendi mi? (fresh session)
                if not await duplicate_filtrele(link):
                    logger.debug(f"⏭️  DB DEDUP: '{haber_title[:40]}...' zaten DB'de")
                    continue

                logger.info(f"🆕 YENİ HABER #{i+1}: '{haber_title[:50]}...'")
                seen_in_batch.add(batch_key)

                # 3. Gemini analizi
                analiz = await gemini_gonder(haber_title, link)

                if analiz is None:
                    # Gemini 3 denemede de yanıt vermedi — haberi atla, loglandı
                    logger.warning(f"Atlandı (Gemini hatası): {haber_title[:60]}")
                    continue

                # 4. DB'ye kaydet — IntegrityError = başka process zaten kaydetti, atla
                haber_source = haber.get('source') or 'Bilinmeyen Kaynak'
                try:
                    async with AsyncSessionLocal() as session:
                        yeni_haber = NewsItem(
                            source=haber_source,
                            original_title=haber_title,
                            original_link=link,
                            ai_summary=analiz
                        )
                        session.add(yeni_haber)
                        await session.commit()
                except IntegrityError:
                    logger.debug(f"Duplicate (race condition): {link[:80]}")
                    continue

                # 5. Telegram'a ilet (sadece DB kaydı başarılıysa)
                await telegram_gonder(haber_title, analiz, haber_source, link)
                await asyncio.sleep(2)

            logger.info("Döngü tamamlandı. 30 dakika uyku moduna geçiliyor...")
            await asyncio.sleep(1800)  # 30 minutes between crawl cycles

        except Exception as e:
            logger.error(f"Crawler Kritik Hatası: {e}", exc_info=True)
            await asyncio.sleep(60)
