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

import json

AXIOM_SYSTEM_PROMPT = """Sen "Axiom OS" bünyesinde çalışan elit ve tarafsız bir finansal analiz ekibisin (Axiom Analiz Ekibi).

GÖREVİN:
Sana verilen ham finansal haberi, eğer sağlanmışsa şirketin finansal verileri (Context) ışığında analiz edip profesyonel içerik üretmektir.

KURALLAR:
1. Üslup: "Analistlerimiz" veya "Ekibimiz" dilini kullan. Rasyonel ol, doğrudan yatırım tavsiyesi verme.
2. Çıktı: SADECE geçerli bir JSON döndür. Başka hiçbir açıklama yazma.

ÇIKTI JSON FORMATI:
{
  "telegram_hook": "Aşağıdaki 3 bölümden oluşan profesyonel Türkçe metin:\n\n📝 **Özet:** Haberin ana fikri (1 cümle).\n\n⚡ **Etki:** Piyasa/Hisse üzerindeki finansal etkisi.\n\n🧠 **Axiom:** Analistlerimizin stratejik görüşü (Örn: 'Mevcut P/E seviyesi göz önüne alındığında...').",
  "dashboard_summary": "Dashboard kullanıcıları için 2-3 paragraflık derinlemesine pazar özeti. Haber ile şirketin finansal durumunu (sağlanmışsa) harmanla.",
  "axiom_analysis": "Hisse için kurumsal risk ve fırsat tespiti (kısa ve eyleme dökülebilir)."
}
"""

def generate_summary_sync(news_title: str, news_link: str, company_context: dict = None) -> dict:
    """Gemini API'sine senkron istek atıp 3-Tier JSON özeti döner."""
    if not GEMINI_API_KEY or "buraya" in GEMINI_API_KEY:
        logger.error("Geçersiz GEMINI_API_KEY")
        return None

    context_str = ""
    if company_context:
        context_str = f"\n\n=== ŞİRKET FİNANSAL KARNESİ (FMP Context) ===\n{json.dumps(company_context, indent=2, ensure_ascii=False)}"

    prompt_text = f"{AXIOM_SYSTEM_PROMPT}\n{context_str}\n\nİncelenecek Haber Başlığı: {news_title}\nİncelenecek Kaynak: {news_link}"

    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1500,
        }
    }

    headers = {"Content-Type": "application/json"}
    MODEL = "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"

    for attempt in range(5):  # 5 deneme
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=20)

            if response.status_code in (503, 429):
                wait = [5, 10, 20, 30, 60][attempt]
                logger.warning(f"[Attempt {attempt+1}] HTTP {response.status_code} - {wait}s bekleniyor...")
                time.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()

            candidates = data.get("candidates", [])
            if not candidates:
                logger.warning(f"[Attempt {attempt+1}] No candidates in response")
                time.sleep(2)
                continue

            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            if not text:
                logger.warning(f"[Attempt {attempt+1}] Empty text in response")
                time.sleep(2)
                continue

            # Robust JSON extraction — handles ```json ... ``` blocks too
            import re
            json_match = re.search(r'\{[\s\S]*\}', text)
            if not json_match:
                logger.warning(f"[Attempt {attempt+1}] No JSON object found in response text")
                time.sleep(2)
                continue

            parsed_json = json.loads(json_match.group())
            required_keys = ["telegram_hook", "dashboard_summary", "axiom_analysis"]
            for k in required_keys:
                if k not in parsed_json:
                    parsed_json[k] = ""
            logger.info(f"✅ AI Özeti oluşturuldu: {news_title[:50]}")
            return parsed_json

        except json.JSONDecodeError as e:
            logger.error(f"[Attempt {attempt+1}] JSON parse hatası: {e}")
            time.sleep(2)
        except Exception as e:
            logger.error(f"[Attempt {attempt+1}] Gemini Hatası: {str(e)}")
            time.sleep(3)

    return None

async def generate_summary(news_title: str, news_link: str, company_context: dict = None) -> dict:
    return await asyncio.to_thread(generate_summary_sync, news_title, news_link, company_context)


# ═══════════════════════════════════════════════════════════════════════════════
# BATCH ANALYSIS — 10 news items in one Gemini call = ~90% quota savings
# ═══════════════════════════════════════════════════════════════════════════════

AXIOM_BATCH_PROMPT = """Sen "Axiom OS" bünyesinde çalışan elit, tarafsız, Bloomberg/Reuters/JP Morgan
düzeyinde bir finansal analiz ekibisin (Axiom Analiz Ekibi).

GÖREVİN:
Sana JSON giriş olarak verilen haber LİSTESİNİ tek tek analiz edip her biri için
profesyonel 3-katmanlı Türkçe içerik üretmektir. Context olarak şirketin finansal
karnesi sağlanmışsa (P/E, ROE, marketCap vs.), analizini mutlaka bu verilerle
harmanla — aksi halde genel geçer cümleler yazma, haberin özünden kalk.

KURALLAR:
1. **Üslup:** Kurumsal, nesnel, "Analistlerimiz" / "Axiom ekibimizin değerlendirmesi"
   gibi kalıplar. Yatırım tavsiyesi (al/sat) ASLA verme. Yerine risk-fırsat,
   göstergeler, piyasa psikolojisi üzerinden konuş.
2. **Türkçe, akıcı, terim:** "FOMC toplantısı", "QT (niceliksel sıkılaşma)",
   "breakeven enflasyon", "forward P/E" gibi terimleri yerinde kullan.
3. **Çıktı:** SADECE geçerli JSON DİZİSİ döndür. Markdown kod bloğu, açıklama,
   başlık YOK. İlk karakter '[', son karakter ']'.
4. **Sıra:** Her haber için bir obje, girdideki "id" ile birebir aynı sırayla.

ÇIKTI ALANLARI (her obje):
  • "telegram_hook" (120-180 kelime) — Aşağıdaki ÜÇ bölümden oluşsun:
       📝 **Özet:** 1-2 cümlede haberin ÖZÜ (kim, ne, nerede, nasıl).
       ⚡ **Etki:** Piyasa/Sektör/Hisse üzerindeki somut etki. Rakam ver
           (ör: "%3-5 baskı", "1.080 dolar direnç"). Jenerik olma.
       🧠 **Axiom:** Analistlerimizin stratejik görüşü. Context verisine
           dayanarak (P/E, ROE, yakın momentum) yorum. Yatırımcının dikkat
           etmesi gereken tek bir tetik veya seviye.
  • "dashboard_summary" (180-280 kelime, 2-3 paragraf) —
       İlk paragraf: haberin bağlamı ve zincirleme etkisi.
       İkinci paragraf: context varsa şirketin güncel finansal pozisyonu
       (marketCap, P/E, ROE, net borç) ışığında analitik yorum; context
       yoksa sektörel/makro kıyas.
       Üçüncü paragraf (opsiyonel): yakın gelecek için izlenecek olaylar
       ve veri takvimi (earnings, CPI, FOMC, merger closing vs.).
  • "axiom_analysis" (60-90 kelime) —
       Kurumsal tarzda risk/fırsat tespiti. Maddeler değil, akıcı metin.
       Somut seviye veya tetikleyici içer ("Ernst & Young denetimi sonucu
       %0,5 marj tıraşı halinde", "10Y ABD tahvil 4,40 üstü tutarsa").

KALİTE ÇITASI — bunlardan kaçın:
  ✗ "Yakından takip edilmeli", "önemli bir gelişme", "dikkatle izlenmeli"
    gibi içi boş, copy-paste jenerik ifadeler.
  ✗ "Yatırımcılar yatırım kararlarını kendileri vermelidir" uyarısı.
  ✗ Aynı cümlenin dashboard_summary ve axiom_analysis'te tekrarı.
  ✗ 40 kelimeden kısa dashboard_summary.

ÇIKTI FORMATI (örnek — yalnızca JSON dizisi):
[
  {
    "id": 101,
    "telegram_hook": "📝 **Özet:** ...\\n\\n⚡ **Etki:** ...\\n\\n🧠 **Axiom:** ...",
    "dashboard_summary": "Paragraf 1 ...\\n\\nParagraf 2 ...\\n\\nParagraf 3 ...",
    "axiom_analysis": "..."
  }
]
"""


def _build_batch_payload(items: list) -> str:
    """items: [{'id': int, 'title': str, 'link': str, 'body': str, 'context': dict|None}, ...]"""
    lines = []
    for item in items:
        ctx_str = ""
        if item.get("context"):
            ctx_str = f"\nContext (finansal karne): {json.dumps(item['context'], ensure_ascii=False)}"
        body_str = ""
        body = (item.get("body") or "").strip()
        if body:
            body_str = f"\nHaber içeriği: {body}"
        lines.append(
            f"[id={item['id']}]\n"
            f"Başlık: {item['title']}\n"
            f"Kaynak: {item.get('link', '')}"
            f"{body_str}"
            f"{ctx_str}"
        )
    return "\n\n---\n\n".join(lines)


def _call_gemini_batch(model: str, prompt_text: str, max_tokens: int, timeout_sec: int) -> dict:
    """
    Tek bir Gemini modeli için batch çağrı. Retry / backoff içerir.
    Return: {id: {telegram_hook, dashboard_summary, axiom_analysis}} veya boş dict.
    429 yerken `__rate_limited__` keyini True set eder — caller fallback'e geçsin.
    """
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",  # JSON output mode
        },
    }

    headers = {"Content-Type": "application/json"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"

    rate_limit_count = 0
    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout_sec)

            if response.status_code == 429:
                rate_limit_count += 1
                wait = [5, 15, 40][attempt]
                logger.warning(f"[{model} attempt {attempt+1}] HTTP 429 - {wait}s bekle")
                time.sleep(wait)
                continue
            if response.status_code == 503:
                wait = [3, 8, 20][attempt]
                logger.warning(f"[{model} attempt {attempt+1}] HTTP 503 - {wait}s bekle")
                time.sleep(wait)
                continue

            response.raise_for_status()
            data = response.json()

            candidates = data.get("candidates", [])
            if not candidates:
                logger.warning(f"[{model} attempt {attempt+1}] No candidates")
                time.sleep(2)
                continue

            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            if not text:
                logger.warning(f"[{model} attempt {attempt+1}] Empty text")
                time.sleep(2)
                continue

            import re
            array_match = re.search(r"\[[\s\S]*\]", text)
            if not array_match:
                logger.warning(f"[{model} attempt {attempt+1}] No JSON array found")
                time.sleep(2)
                continue

            # strict=False → string değerlerin içindeki \n, \t gibi escape'lenmemiş
            # control karakterlerini tolere et (Gemini bazen sarmalanmış text'te
            # ham \n bırakıyor). Ayrıca ham kontrol karakterlerini de temizle.
            raw = array_match.group()
            # ASCII control chars (except \n \r \t) silinsin → JSON-safe
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
            try:
                parsed = json.loads(cleaned, strict=False)
            except json.JSONDecodeError:
                # Son çare: string değerlerin içinde kalan ham \n → \\n escape et
                cleaned2 = re.sub(
                    r'("(?:[^"\\]|\\.)*?)(\n)',
                    lambda m: m.group(1) + "\\n",
                    cleaned,
                )
                parsed = json.loads(cleaned2, strict=False)
            if not isinstance(parsed, list):
                logger.warning(f"[{model} attempt {attempt+1}] Parsed non-list")
                time.sleep(2)
                continue

            result: dict = {}
            for obj in parsed:
                if not isinstance(obj, dict):
                    continue
                obj_id = obj.get("id")
                if obj_id is None:
                    continue
                result[obj_id] = {
                    "telegram_hook": (obj.get("telegram_hook") or "").strip(),
                    "dashboard_summary": (obj.get("dashboard_summary") or "").strip(),
                    "axiom_analysis": (obj.get("axiom_analysis") or "").strip(),
                }

            logger.info(f"✅ [{model}] Batch: {len(result)} obje")
            return result

        except json.JSONDecodeError as e:
            logger.error(f"[{model} attempt {attempt+1}] JSON parse: {e}")
            time.sleep(2)
        except Exception as e:
            logger.error(f"[{model} attempt {attempt+1}] {e}")
            time.sleep(3)

    # 3 deneme de 429 ise caller'a sinyal
    return {"__rate_limited__": True} if rate_limit_count >= 2 else {}


# Primary & fallback model — primary 429 çakılırsa lite model devreye girer.
# gemini-2.0-flash kaliteli ama sıkı quota. gemini-2.5-flash-lite daha bol quota.
_PRIMARY_MODEL = os.getenv("AXIOM_GEMINI_PRIMARY", "gemini-2.0-flash")
_FALLBACK_MODEL = os.getenv("AXIOM_GEMINI_FALLBACK", "gemini-2.5-flash-lite")


def generate_batch_summaries_sync(items: list) -> dict:
    """
    Birden çok haberi tek Gemini çağrısında analiz eder. Primary model 429 yerse
    otomatik fallback model'e geçer.

    Args:
        items: [{'id': int, 'title': str, 'link': str, 'context': dict|None}, ...]

    Returns:
        {id: {telegram_hook, dashboard_summary, axiom_analysis}, ...}
    """
    if not items:
        return {}

    if not GEMINI_API_KEY or "buraya" in GEMINI_API_KEY:
        logger.error("Geçersiz GEMINI_API_KEY (batch)")
        return {}

    news_block = _build_batch_payload(items)
    prompt_text = f"{AXIOM_BATCH_PROMPT}\n\n=== ANALİZ EDİLECEK HABERLER ===\n\n{news_block}"

    # 1) Primary model
    result = _call_gemini_batch(_PRIMARY_MODEL, prompt_text, max_tokens=8192, timeout_sec=45)

    # Rate-limited ise fallback
    if result.get("__rate_limited__"):
        logger.warning(f"Primary '{_PRIMARY_MODEL}' rate-limited, fallback '{_FALLBACK_MODEL}' deneniyor...")
        result = _call_gemini_batch(_FALLBACK_MODEL, prompt_text, max_tokens=8192, timeout_sec=45)

    # Fallback da sinyal verdiyse temizle
    result.pop("__rate_limited__", None)
    return result


async def generate_batch_summaries(items: list) -> dict:
    """Async wrapper: batch analizi thread havuzunda çalıştırır."""
    return await asyncio.to_thread(generate_batch_summaries_sync, items)
