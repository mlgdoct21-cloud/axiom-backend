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


# ─────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER — per-model quota cooldown
# ─────────────────────────────────────────────────────────────────────────────
# Gemini free-tier günlük RPD (Requests Per Day) limiti dolunca model "exhausted"
# durumuna düşer ve o günün geri kalanında sürekli 429 döner. Her cycle'da onu
# tekrar denersek 30+ saniye boşa beklemiş oluruz. Circuit breaker: bir model
# 429/RESOURCE_EXHAUSTED dönerse `_MODEL_COOLDOWN_SEC` süresince o modeli atla.
_MODEL_COOLDOWN_SEC = int(os.getenv("AXIOM_MODEL_COOLDOWN_SEC", "900"))  # 15 dk
_model_cooldown_until: dict = {}  # {model_name: unix_ts_resume_at}


def _is_model_cool(model: str) -> bool:
    """True if model is NOT in cooldown (safe to call)."""
    until = _model_cooldown_until.get(model, 0)
    return time.time() >= until


def _trip_breaker(model: str, reason: str = "429") -> None:
    """Model'i cooldown'a al. O modele bu süre boyunca istek atılmaz."""
    resume_at = time.time() + _MODEL_COOLDOWN_SEC
    _model_cooldown_until[model] = resume_at
    logger.warning(
        f"🔴 Circuit breaker TRIPPED: '{model}' ({reason}) — "
        f"{_MODEL_COOLDOWN_SEC}s cooldown (resume @ {time.strftime('%H:%M:%S', time.localtime(resume_at))})"
    )


def _reset_breaker(model: str) -> None:
    if model in _model_cooldown_until:
        _model_cooldown_until.pop(model, None)
        logger.info(f"🟢 Circuit breaker RESET: '{model}' — başarılı çağrı.")


def _call_gemini_batch(model: str, prompt_text: str, max_tokens: int, timeout_sec: int) -> dict:
    """
    Tek bir Gemini modeli için batch çağrı.

    Rate-limit stratejisi (production-grade):
      - İlk 429 → anında breaker trip et, boş dict dön. Caller fallback'e geçsin.
      - Burada retry yok — çünkü 429 kota bitmesi demektir, saniyeler içinde çözülmez.
      - 503 (overloaded) → 1 retry, kısa bekleme (geçici sorun).
      - JSON parse / network hatası → 1 retry.

    Return:
      - {"__rate_limited__": True} eğer 429 alındıysa.
      - {id: {telegram_hook, dashboard_summary, axiom_analysis}, ...} başarıda.
      - {} geçici hata/parse sorunu (caller next cycle'da tekrar dener).
    """
    # Circuit breaker kontrolü — cooldown'daki modeli hiç arama
    if not _is_model_cool(model):
        remaining = int(_model_cooldown_until[model] - time.time())
        logger.debug(f"[{model}] Cooldown'da, {remaining}s sonra açılacak.")
        return {"__rate_limited__": True}

    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }

    headers = {"Content-Type": "application/json"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"

    # Toplam 2 attempt (503/geçici hata için). 429 ANINDA circuit breaker'a gider.
    for attempt in range(2):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout_sec)

            if response.status_code == 429:
                # Kota bitmesi — retry boşuna. Breaker trip + fallback'e yönlendir.
                _trip_breaker(model, reason="HTTP 429 quota exhausted")
                return {"__rate_limited__": True}

            if response.status_code == 503:
                # Overloaded — kısa bekleme + 1 retry.
                if attempt == 0:
                    logger.warning(f"[{model}] HTTP 503 overloaded — 5s retry")
                    time.sleep(5)
                    continue
                logger.warning(f"[{model}] HTTP 503 2 deneme de başarısız, skip.")
                return {}

            response.raise_for_status()
            data = response.json()

            candidates = data.get("candidates", [])
            if not candidates:
                logger.warning(f"[{model}] No candidates (attempt {attempt+1})")
                if attempt == 0:
                    time.sleep(2)
                    continue
                return {}

            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            if not text:
                logger.warning(f"[{model}] Empty text (attempt {attempt+1})")
                if attempt == 0:
                    time.sleep(2)
                    continue
                return {}

            import re

            # Önce JSON array `[...]` dene. Yoksa obje `{...}` dene —
            # bazı modeller (ör. 2.5-flash-lite) responseMimeType=json olsa
            # bile array yerine `{"items":[...]}` / `{"0":{...}, "1":{...}}`
            # şeklinde obje döndürebiliyor.
            array_match = re.search(r"\[[\s\S]*\]", text)
            object_match = re.search(r"\{[\s\S]*\}", text)

            if not array_match and not object_match:
                logger.warning(
                    f"[{model}] No JSON in response (attempt {attempt+1}): "
                    f"{text[:200]!r}"
                )
                if attempt == 0:
                    time.sleep(2)
                    continue
                return {}

            # Array varsa array'i tercih et, yoksa obje
            raw = array_match.group() if array_match else object_match.group()
            cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)

            def _try_parse(s: str):
                """3 aşamalı permissive JSON parse:
                1) vanilla json.loads
                2) escape unterminated newlines inside strings
                3) quote JS-style unquoted keys (2.5-flash-lite bug)
                   + convert single→double quotes
                   + strip trailing commas before ] or }
                """
                try:
                    return json.loads(s, strict=False)
                except json.JSONDecodeError:
                    pass
                # Stage 2: escape raw newlines inside strings
                s2 = re.sub(
                    r'("(?:[^"\\]|\\.)*?)(\n)',
                    lambda m: m.group(1) + "\\n",
                    s,
                )
                try:
                    return json.loads(s2, strict=False)
                except json.JSONDecodeError:
                    pass
                # Stage 3: JS-style repair
                #   - unquoted keys:  { id: 1 }  → { "id": 1 }
                #   - single-quoted strings:  'x' → "x"  (naive but works for simple values)
                #   - trailing commas:  [1,2,] → [1,2]
                s3 = re.sub(
                    r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)',
                    r'\1"\2"\3',
                    s2,
                )
                s3 = re.sub(r",(\s*[\]}])", r"\1", s3)
                return json.loads(s3, strict=False)

            try:
                parsed = _try_parse(cleaned)
            except json.JSONDecodeError as e:
                # Diagnostic: what did the model actually return?
                logger.error(
                    f"[{model}] JSON repair failed (attempt {attempt+1}): {e} | "
                    f"raw[:500]={cleaned[:500]!r}"
                )
                raise

            # Dict döndüyse içindeki array'i ya da values'i çıkar
            if isinstance(parsed, dict):
                # 1) Yaygın wrapper field isimleri
                for key in ("items", "results", "data", "batch", "analyses", "news"):
                    val = parsed.get(key)
                    if isinstance(val, list):
                        parsed = val
                        break
                else:
                    # 2) Key'ler "0","1","2" gibi numerik/id → values al
                    values = list(parsed.values())
                    if values and all(isinstance(v, dict) for v in values):
                        parsed = values
                    else:
                        logger.warning(
                            f"[{model}] Obje yapısı anlaşılamadı (keys={list(parsed.keys())[:5]}): "
                            f"{text[:300]!r}"
                        )
                        return {}

            if not isinstance(parsed, list):
                logger.warning(f"[{model}] Parsed non-list: {type(parsed).__name__}")
                return {}

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
            _reset_breaker(model)  # başarı → cooldown varsa temizle
            return result

        except json.JSONDecodeError as e:
            logger.error(f"[{model}] JSON parse (attempt {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(2)
                continue
            return {}
        except requests.Timeout:
            logger.warning(f"[{model}] Timeout (attempt {attempt+1})")
            if attempt == 0:
                continue
            return {}
        except Exception as e:
            logger.error(f"[{model}] Unexpected error (attempt {attempt+1}): {e}")
            if attempt == 0:
                time.sleep(3)
                continue
            return {}

    return {}


# Primary & fallback models — ENV-override destekli.
#
# DEFAULT STRATEJİ (production):
#   - PRIMARY = gemini-2.5-flash-lite (free-tier 10k RPD, 30 RPM — yüksek throughput)
#   - FALLBACK = gemini-2.0-flash (free-tier 1500 RPD, 15 RPM — daha kaliteli
#     ama sınırlı; primary cooldown'dayken backup görevi görür)
#
# Neden swap edildi:
#   Eski default 2.0-flash primary idi, 1500 RPD hızla tükeniyordu (pipeline
#   ~1000-3000 haber/gün üretiyor). Her cycle'da 429 alıp 40s bekliyorduk.
#   2.5-flash-lite primary yapıldı çünkü 6-7x daha bol quota + yeterince kaliteli.
_PRIMARY_MODEL = os.getenv("AXIOM_GEMINI_PRIMARY", "gemini-2.5-flash-lite")
_FALLBACK_MODEL = os.getenv("AXIOM_GEMINI_FALLBACK", "gemini-2.0-flash")


def get_model_status() -> dict:
    """Monitoring endpoint için: hangi model cooldown'da?"""
    now = time.time()
    return {
        "primary_model": _PRIMARY_MODEL,
        "fallback_model": _FALLBACK_MODEL,
        "cooldowns": {
            model: {
                "until_ts": until,
                "remaining_sec": max(0, int(until - now)),
                "cooling_down": until > now,
            }
            for model, until in _model_cooldown_until.items()
        },
    }


def generate_batch_summaries_sync(items: list) -> dict:
    """
    Birden çok haberi tek Gemini çağrısında analiz eder.

    Akış:
      1) Primary model dene. Başarı → dön.
      2) Primary rate-limited (breaker trip) → fallback dene.
      3) Fallback da rate-limited → boş dön (caller next cycle'da tekrar dener).

    Args:
        items: [{'id': int, 'title': str, 'link': str, 'body': str, 'context': dict|None}, ...]

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

    # 2) Primary rate-limited → fallback'e geç (anında, bekleme yok)
    if result.get("__rate_limited__"):
        logger.info(f"Primary '{_PRIMARY_MODEL}' cooldown'da, fallback '{_FALLBACK_MODEL}' deneniyor...")
        result = _call_gemini_batch(_FALLBACK_MODEL, prompt_text, max_tokens=8192, timeout_sec=45)

    # 3) Her iki model de cooldown'daysa temiz dict dön (caller retry cycle'da)
    if result.get("__rate_limited__"):
        logger.warning(
            "⚠️ Her iki Gemini model de cooldown'da. Bu cycle atlanıyor, "
            "sonraki batch_analyze_loop iteration'da tekrar denenecek."
        )
        return {}

    return result


async def generate_batch_summaries(items: list) -> dict:
    """Async wrapper: batch analizi thread havuzunda çalıştırır."""
    return await asyncio.to_thread(generate_batch_summaries_sync, items)
