import feedparser
import asyncio
import re
from datetime import datetime
from core.logger import get_logger


_HTML_RE = re.compile(r"<[^>]+>")


def _clean_body(raw: str, max_len: int = 800) -> str:
    """RSS summary HTML'ini temizler, AI context için plaintext döner."""
    if not raw:
        return ""
    text = _HTML_RE.sub(" ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max_len - 1] + "…" if len(text) > max_len else text

logger = get_logger("rss_service")

# ─── HİBRİT STRATEJİ (v3.0) ───────────────────────────────────────────────────
# FMP artık 5 endpoint paralel (stock/press/general/crypto/forex) → US+global
# haberleri kapsıyor. RSS bu nedenle SADECE FMP'nin kör noktalarını kapatıyor:
#
#   • Türkçe finans (BIST, TCMB, Türk makro) — FMP'de YOK
#     └─ Bloomberg HT, Dünya Gazetesi
#
#   • Kripto-native muhabir ağı (on-chain analiz, DeFi, NFT)
#     └─ CoinDesk, Cointelegraph (FMP crypto endpoint'i daha çok price-action)
#
# Yahoo Finance RSS ve Investing.com RSS kaldırıldı — FMP bunları zaten
# agregör olarak içeriyor (çift kayıt = gürültü).
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_RSS_FEEDS = {
    # Türkçe finans — BIST haberleri için kritik (FMP Türkçe kaynak taşımaz)
    "Bloomberg HT":   "https://www.bloomberght.com/rss",
    "Dünya Gazetesi": "https://www.dunya.com/rss?dunya",

    # Kripto-native — on-chain, DeFi, regülasyon perspektifi
    "CoinDesk":      "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
}

# Kaynak başına kaç entry alınsın. Eski sürümde 5 idi; hibrit modda RSS
# daha nadir sorunlu olduğu için 10'a çıkardık — BIST haberleri zaten
# düşük frekansta, fazla alsak bile dedup filtrelemesi zaten yapıyor.
MAX_ENTRIES_PER_FEED = 10


def fetch_feed_sync(source_name, url):
    """
    Belirli bir URL'den gelen RSS akışını okur ve standart bir liste döner.
    Feedparser doğrudan URL'den okuma yapabildiği için senkron çalışır.
    """
    try:
        parsed_feed = feedparser.parse(url)
        news_list = []

        for entry in parsed_feed.entries[:MAX_ENTRIES_PER_FEED]:
            body = _clean_body(entry.get("summary") or entry.get("description") or "")
            news_list.append({
                "source": source_name,
                "title": entry.get("title", "Başlık Yok"),
                "link": entry.get("link", ""),
                "body": body,
                "published": entry.get("published", str(datetime.now())),
            })

        return news_list
    except Exception as e:
        logger.error(f"RSS feed hatası ({source_name}): {e}")
        return []

async def fetch_all_feeds(sources: dict = None):
    """
    Tüm kaynakları asenkron (birbirini beklemeden paralel) olarak tarar.
    sources: {name: url} dict. None ise DEFAULT_RSS_FEEDS kullanılır.
    """
    if sources is None:
        sources = DEFAULT_RSS_FEEDS
    tasks = []
    for source_name, url in sources.items():
        tasks.append(asyncio.to_thread(fetch_feed_sync, source_name, url))
    
    # Tüm kaynakların taranmasını bekle ve sonuçları birleştir
    results = await asyncio.gather(*tasks)
    
    # Haberleri tek bir havuzda (düz listede) topluyoruz (Make.com Iterator mantığı)
    all_news = []
    for source_news in results:
        all_news.extend(source_news)
        
    return all_news

# Yerel test etmek isterseniz dosyayı doğrudan çalıştırabilirsiniz:
if __name__ == "__main__":
    async def test():
        print("RSS kaynakları taranıyor...")
        news = await fetch_all_feeds(DEFAULT_RSS_FEEDS)
        for i, n in enumerate(news, 1):
            print(f"{i}. [{n['source']}] {n['title']}")
            print(f"   Link: {n['link']}\n")
    
    asyncio.run(test())
