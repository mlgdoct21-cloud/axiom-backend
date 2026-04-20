import os
import time
import requests
import asyncio
from dotenv import load_dotenv
from core.logger import get_logger

load_dotenv()

logger = get_logger("ai_service")

# API Key'in sonundaki olası boşlukları, görünmez karakterleri .strip() ile mutlaka temizliyoruz (Windows sorunlarına karşı)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

AXIOM_SYSTEM_PROMPT = """Sen "Axiom OS" isimli elit ve tarafsız bir algoritmik finansal analistsin (FinLens).

GÖREVİN:
Sana verilen ham finansal haberi, yatırımcıların zamanını korumak ve piyasanın gürültüsünü filtrelemek amacıyla TAM OLARAK 3 MADDE halinde özetlemektir.

KURALLAR:
1. Üslup: Asla duygusal olma, abartılı sıfatlar ("uçtu", "çöktü", "harika") kullanma. Soğuk, rasyonel, sadece verilerle konuşan üst düzey bir İngiliz fon yöneticisi gibi ol.
2. Regülasyon: Hiçbir şekilde doğrudan yatırım tavsiyesi (AL, SAT, TUT vb.) verme. SPK kurallarına katı şekilde uy.
3. Çıktı: Selamlama ve kapanış yapma. Markdown kullanma (**, *, # gibi). Sadece düz metin kullan.
4. Her madde maksimum 2 cümle olsun ve tam olarak aşağıdaki format ile bitsin.

ÇIKTI FORMATIN (bu formatın DIŞINA çıkma):
🔍 Ana Gelişme: [Haberin en net teknik özeti. Oranlar, isimler ve rakamlar.]
⚙️ Piyasa Dinamikleri: [Bu gelişmenin ilgili varlık sınıfı üzerindeki mekanik etkisi.]
📊 Axiom İçgörüsü: [Geçmiş emsal veriler veya bu verinin ardındaki rasyonel gerçeklik.]
"""

def generate_summary_sync(news_title: str, news_link: str) -> str:
    """Gemini API'sine senkron istek atıp özeti döner. Hata durumunda 3 kez retry yapar."""
    if not GEMINI_API_KEY or "buraya" in GEMINI_API_KEY:
        return "⚠️ Hata: Lütfen .env dosyasına geçerli bir GEMINI_API_KEY girin."

    # Primary: gemini-2.5-flash-lite (fast, stable, free tier)
    # Fallback: gemini-2.5-flash (slightly slower but also stable)
    MODELS = [
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
    ]

    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": f"{AXIOM_SYSTEM_PROMPT}\n\nİncelenecek Haber Başlığı: {news_title}\nİncelenecek Kaynak: {news_link}"
                    }
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1500
        }
    }

    headers = {"Content-Type": "application/json"}

    for attempt in range(3):
        model = MODELS[min(attempt, len(MODELS) - 1)]
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=12)

            if response.status_code in (503, 429):
                logger.warning(f"Gemini {response.status_code} ({model}, deneme {attempt+1}/3), 3s bekleniyor...")
                if attempt < 2:
                    time.sleep(3)
                continue

            response.raise_for_status()
            data = response.json()

            if "candidates" in data and len(data["candidates"]) > 0:
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if text:
                    logger.info(f"Gemini yanıtı alındı ({model}, deneme {attempt+1})")
                    return text

            logger.warning(f"Boş yanıt ({model}, deneme {attempt+1}/3)")
        except Exception as e:
            logger.error(f"Gemini Hatası ({model}, deneme {attempt+1}/3): {str(e)}")
            if attempt < 2:
                time.sleep(3)

    logger.error("3 denemede de yanıt alınamadı, haber atlanıyor.")
    return None

async def generate_summary(news_title: str, news_link: str) -> str:
    return await asyncio.to_thread(generate_summary_sync, news_title, news_link)
