import asyncio
from services.rss_service import fetch_all_feeds
from services.ai_service import generate_summary
from core.logger import get_logger
import os
from dotenv import load_dotenv

logger = get_logger("test_flow")

async def main():
    load_dotenv()
    if not os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY") == "buraya_kendi_api_anahtarinizi_yazin":
        logger.warning("GEMINI_API_KEY .env dosyasına eklenmemiş. Haberler çekilecek ancak AI analizi yapılamayacak.")

    logger.info("RSS Kaynaklarından haberler çekiliyor...")
    news_list = await fetch_all_feeds()

    if not news_list:
        logger.error("Hiç haber bulunamadı.")
        return

    logger.info(f"Toplam {len(news_list)} haber çekildi.")
    logger.info("İlk haber örnek olarak seçiliyor ve FinLens algoritmasına gönderiliyor...")

    sample_news = news_list[0]
    print(f"\nHAM HABER: [{sample_news['source']}] {sample_news['title']}\n")
    print("="*60)
    print("AXIOM OS (FinLens) İŞLİYOR...\n")

    summary = await generate_summary(sample_news['title'], sample_news['link'])

    # Telegram'da kullanıcıya gidecek Nihai Mesaj Formatı
    final_message = f"{summary}\n\n🔗 **Kaynak:** [{sample_news['source']}]({sample_news['link']})"

    print(final_message)
    print("\n" + "="*60)

if __name__ == "__main__":
    asyncio.run(main())
