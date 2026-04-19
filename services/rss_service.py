import feedparser
import asyncio
from datetime import datetime
from core.logger import get_logger

logger = get_logger("rss_service")

# Varsayılan kaynaklar (DB boşsa veya standalone test için)
DEFAULT_RSS_FEEDS = {
    "Investing.com": "https://www.investing.com/rss/news.rss",
    "Yahoo Finance (AAPL)": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US",
    "Bloomberg HT": "https://www.bloomberght.com/rss"
}

def fetch_feed_sync(source_name, url):
    """
    Belirli bir URL'den gelen RSS akışını okur ve standart bir liste döner.
    Feedparser doğrudan URL'den okuma yapabildiği için senkron çalışır.
    """
    try:
        parsed_feed = feedparser.parse(url)
        news_list = []
        
        # En yeni 5 haberi alalım (test/MVP aşaması için yığılmayı önlemek amacıyla)
        for entry in parsed_feed.entries[:5]: 
            news_list.append({
                "source": source_name,
                "title": entry.get("title", "Başlık Yok"),
                "link": entry.get("link", ""),
                "published": entry.get("published", str(datetime.now()))
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
