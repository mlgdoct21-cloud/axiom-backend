"""
Axiom Haber Motoru — Profesyonel İki Aşamalı Pipeline

ARCHITECTURE
────────────
Eskisi gibi "fetch → Gemini → save → telegram" tek döngü değil, üç bağımsız döngü:

  1) fast_fetch_loop  (60 sn)
       FMP (öncelik) → RSS (fallback) → dedup → DB'ye RAW kaydet
       Henüz Gemini çağrısı YOK. analyzed=False. Kullanıcı haberi KAÇIRMAZ.

  2) batch_analyze_loop (5 dk)
       analyzed=False haberlerden 10 tanesini al → tek Gemini çağrısı (batch)
       → dashboard_summary + telegram_hook + axiom_analysis DB'ye yaz
       → event bus'a publish (dashboard SSE anında alır)
       → urgent ise instant telegram broadcast, normal ise digest kuyruğuna ekle
       %90 Gemini kota tasarrufu.

  3) digest_loop (15 dk)
       Son 15 dakikada biriken normal haberleri tek bir özet mesaj olarak
       kullanıcılara gönder. Spam önler, önemli haberi bekletmez (urgent anında).

Her döngü kendi supervisor'ı ile kapsanır; çökerse kendini yeniden başlatır.

Data flow:
    FMP/RSS ──► fast_fetch_loop ──► DB (analyzed=False)
                                        │
                                        └──► batch_analyze_loop ──► DB (analyzed=True)
                                                                      │
                                                                      ├──► event_bus.publish (SSE)
                                                                      ├──► telegram_instant (urgent)
                                                                      └──► digest_queue (normal)
                                                                                │
                                                                                └──► digest_loop
"""
import asyncio
import os
import html
import random
import re
import hashlib
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple, Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
from sqlalchemy import update

from core.database import AsyncSessionLocal
from core.logger import get_logger
from models.news import NewsItem
from models.user import User
from models.source import Source
from services.rss_service import fetch_all_feeds, DEFAULT_RSS_FEEDS
from services.fmp_service import fetch_fmp_news, fetch_stock_snapshot
from services.ai_service import generate_batch_summaries
from services.telegram_bot import send_telegram_message
from services.event_bus import bus as news_bus

logger = get_logger("crawler")

# ── Yapılandırma ──────────────────────────────────────────────────────────────
# Dashboard'ın "sürekli akış" hissini vermesi için fetch+analyze cycle'ları kısa,
# digest ise sadece non-urgent için kısa bekletme yapar (kullanıcı yenilemek
# zorunda kalmasın; ama urgent'lar anında, normaller de 3 dk içinde gitsin).
# Day 28 #10 — yayın akışı sırasında haber gecikmelerini minimize:
# fetch 30s→15s, analyze 60s→30s, SSE age 10min→5min.
# Worst-case latency 63s → ~25s. Gemini cost +~$0.01/gün.
FAST_FETCH_INTERVAL_SEC = int(os.getenv("AXIOM_FAST_FETCH_SEC", "15"))  # 15 sn
BATCH_ANALYZE_INTERVAL_SEC = int(os.getenv("AXIOM_BATCH_ANALYZE_SEC", "30"))  # 30 sn
DIGEST_INTERVAL_SEC = int(os.getenv("AXIOM_DIGEST_SEC", "180"))  # 3 dk
# Gemini çağrı başına haber adedi. 15 haber ~4-5K token prompt + 4-8K token yanıt.
# Daha büyük batch = daha az API çağrısı = daha az quota tüketimi.
# 30s interval × 15 haber = 1800 haber/saat teorik. Pratik (bazı cycle'lar
# Gemini overloaded döner) ~1000-1400 haber/saat.
BATCH_SIZE = int(os.getenv("AXIOM_BATCH_SIZE", "15"))
FMP_FALLBACK_THRESHOLD = int(os.getenv("AXIOM_FMP_FALLBACK_MIN", "5"))  # FMP bundan az dönerse RSS ekle
# SSE canlı akış tazelik penceresi: bu yaştan eski analizler dashboard'a
# anlık olarak itilmez (DB'de kalır, feed endpoint reload'unda erişilir).
# 10dk → 5dk: backlog uzun sürerse eski item'lar SSE'den çıkmasın.
SSE_MAX_AGE_MIN = int(os.getenv("AXIOM_SSE_MAX_AGE_MIN", "5"))

# Urgent keywords — acil broadcast tetikleyicileri (başlıkta aranır, lowercase)
URGENT_KEYWORDS = [
    "breaking", "flash", "urgent", "acil", "son dakika", "flaş",
    "crash", "çöküş", "dump", "plunge", "collapse", "sert düşüş",
    "halt", "halt trading", "suspended", "durduruldu",
    "bankrupt", "iflas", "default",
    "hack", "exploit", "drain", "rug",
    "fed decision", "fed announces", "rate decision", "faiz kararı",
    "earnings beat", "earnings miss", "guidance cut",
    "merger announced", "acquisition announced",
    "sec charges", "lawsuit", "dava",
]

AXIOM_DASHBOARD_URL = os.getenv("AXIOM_DASHBOARD_URL", "https://axiom-dashboard.vercel.app")

# ── CTA (Marketing) mesajları ─────────────────────────────────────────────────
CTA_TAG_PROMPTS = [
    "\n\n💡 <b>AXIOM İpucu:</b> Çok mu fazla haber geliyor? /tags ile sadece ilgilendiğin konuları seç.",
    "\n\n🎯 <b>Kişiselleştir:</b> /takip AAPL yazarak favori hisselerini takip listesine ekle.",
    "\n\n⚙️ <b>Filtrele:</b> /tags menüsünden BTC, Altın, BIST gibi ilgi alanlarını seç.",
]

CTA_DASHBOARD_PROMPTS = [
    f"\n\n🌐 <b>Axiom Dashboard:</b> 3-katmanlı analiz için → <a href='{AXIOM_DASHBOARD_URL}'>Bağlan</a>",
    f"\n\n📊 <b>Derin Analiz:</b> Uzman analistlerimizin raporu → <a href='{AXIOM_DASHBOARD_URL}'>İncele</a>",
    f"\n\n🛡️ <b>Risk Radar:</b> Axiom analistleri bu gelişmeyi nasıl puanladı → <a href='{AXIOM_DASHBOARD_URL}'>Radar</a>",
]

# ── Tag eşleşmeleri ───────────────────────────────────────────────────────────
# Geniş keyword listesi: her tag için hem Türkçe hem İngilizce, sektör, şirket ve
# popüler sembolleri kapsar. "Hisse" gibi geniş kavramlar için CEO/earnings/
# company/nasdaq/apple/microsoft gibi en çok karşılaşılan kelimeler eklendi ki
# finansal haberlerin büyük bölümü match etsin.
TAG_KEYWORDS = {
    "BTC":    [
        "btc", "bitcoin", "satoshi", "lightning network", "halving",
        "btcusd", "btcusdt", "btcusdc",
    ],
    "Altın":  ["altın", "altin", "gold", "xau", "xauusd", "altın ons", "ons altın", "gram altın"],
    "BIST":   [
        "bist", "borsa istanbul", "bist 100", "bist30", "thyao", "garan", "akbnk",
        "eregl", "aselsan", "asels", "kchol", "sahol", "tuprs", "sise", "toasō",
        "froto", "pgsus", "kozal", "enjsa", "yapı kredi", "ykbnk",
    ],
    "Dolar":  ["dolar", "dollar", "usd", "dxy", "greenback", "usd/try", "usdtry"],
    "Faiz":   [
        "faiz", "interest rate", "fed funds", "tcmb", "rate hike", "rate cut",
        "rate decision", "policy rate", "benchmark rate", "ecb rate",
    ],
    "Fed":    [
        "fed", "fomc", "powell", "federal reserve", "jerome powell",
        "fed meeting", "fed decision", "quantitative easing", "taper",
    ],
    "Euro":   ["euro", "eur", "eur/usd", "eurusd", "ecb", "european central bank"],
    "Petrol": [
        "petrol", "oil", "brent", "wti", "opec", "crude", "barrel", "pipeline",
        "exxon", "chevron", "shell", "bp", "oil price", "refinery",
    ],
    "Kripto": [
        # Full names (specific enough)
        "kripto", "crypto", "ethereum", "solana", "blockchain",
        "defi", "nft", "altcoin", "stablecoin", "binance",
        "coinbase", "ripple", "cardano", "dogecoin",
        "avalanche", "polygon", "chainlink", "web3",
        # Tickers (word-boundary match → "eth" matches "ETH" but NOT "ethan"/"whether")
        "eth", "sol", "ada", "avax", "matic", "xrp", "doge", "usdt", "usdc",
        # Trading pairs (concatenated symbols like BTCUSDT)
        "ethusd", "ethusdt", "ethusdc", "solusd", "solusdt",
        "adausd", "adausdt", "avaxusd", "avaxusdt",
        "xrpusd", "xrpusdt", "dogeusd", "dogeusdt",
        "maticusd", "maticusdt", "linkusd", "linkusdt",
        # NOT: "link", "token", "bridge" removed — too generic even with \b
    ],
    "Hisse":  [
        "hisse", "stock", "stocks", "equity", "equities", "shares", "share price",
        "s&p", "s&p 500", "nasdaq", "dow jones", "russell", "ftse", "dax",
        "earnings", "guidance", "revenue", "ceo", "cfo", "ipo", "buyback",
        "dividend", "merger", "acquisition", "spin-off", "wall street",
        "market cap", "analyst", "upgrade", "downgrade", "price target",
        "apple", "microsoft", "google", "alphabet", "amazon", "meta", "tesla",
        "nvidia", "aapl", "msft", "goog", "googl", "amzn", "tsla", "nvda",
        "form 8k", "form 13f", "form 10k", "form 10q",  # SEC filings = equity
        # NOT: "dow" removed — matches "download"/"showdown" under substring, OK under \b ama kısaltma zaten "dow jones" kapsiyor
    ],
}


def _keyword_in_haystack(keyword: str, haystack: str) -> bool:
    """Word-boundary match — 'eth' matches 'ETH was up' ama 'whether'/'ethan' degil.
    Uzun keyword'ler (≥5 char) icin substring yeterince benzersiz; kisalarda \b zorunlu.
    Bosluk iceren keyword'ler (ör: 'fed funds') substring kalir — \b icindeki space zaten
    boundary'dir.
    """
    kw = keyword.strip().lower()
    if not kw:
        return False
    # \b word-boundary regex — ASCII word chars icin calisir; Turkish char'li
    # keyword'ler zaten 5+ char oldugundan substring yeterli.
    try:
        return re.search(rf"\b{re.escape(kw)}\b", haystack) is not None
    except re.error:
        # Guvenlik: regex hatasinda substring'e dus
        return kw in haystack


# ═══════════════════════════════════════════════════════════════════════════════
# Yardımcı fonksiyonlar
# ═══════════════════════════════════════════════════════════════════════════════

def is_urgent_title(title: str) -> bool:
    """Başlık urgent kelime içeriyor mu?"""
    if not title:
        return False
    t = title.lower()
    return any(kw in t for kw in URGENT_KEYWORDS)


# ═══════════════════════════════════════════════════════════════════════════════
# Deduplication helpers — aynı haberin 3 kez eklenmesini engeller
# ═══════════════════════════════════════════════════════════════════════════════

# Title stop-words (ignore bunları while normalizing)
_TITLE_STOPWORDS = {
    "the", "a", "an", "to", "of", "on", "in", "for", "and", "or", "as", "at",
    "by", "with", "from", "is", "are", "be", "been", "has", "have", "had",
    "will", "would", "can", "could", "should", "may", "this", "that", "these",
    "those", "it", "its", "was", "were", "after", "before", "over",
    "ve", "ile", "için", "bir", "bu", "şu", "o", "da", "de", "ki",
}

# URL query params to strip (tracking noise)
_URL_STRIP_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "source", "ref", "ref_src", "feature", "taid", "ncid", "guccounter",
    "soc_src", "soc_trk", ".tsrc", "fr", "yptr",
}


def normalize_url(url: str) -> str:
    """URL'yi tracking param ve fragment olmadan normalize eder."""
    if not url or url == "#":
        return ""
    try:
        p = urlparse(url.strip())
        # Query params'ı temizle
        kept = [(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _URL_STRIP_PARAMS]
        new_query = urlencode(kept)
        # Fragment'ı at, trailing slash normalize et
        path = p.path.rstrip("/") or "/"
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", new_query, ""))
    except Exception:
        return url.strip()


def normalize_title(title: str) -> str:
    """
    Başlığı fuzzy dedup için normalize eder:
    - lowercase, emoji/noktalama temizle
    - stopword'leri çıkar
    - alphabetic kelimelerin alfabetik sırada birleşimi
    """
    if not title:
        return ""
    t = title.lower()
    # Alfanumerik dışındakileri boşluğa çevir
    t = re.sub(r"[^a-z0-9çğıöşüâîû\s]", " ", t)
    # Kelimeleri al, stopword ve kısa olanları (<3) at
    words = [w for w in t.split() if len(w) >= 3 and w not in _TITLE_STOPWORDS]
    # İlk 10 önemli kelimenin sıralanmış hali → küçük varyasyonlara dayanıklı imza
    words = sorted(set(words))[:10]
    return " ".join(words)


def title_hash(title: str) -> str:
    """Normalize edilmiş başlığın 16-char hash'i — DB/cache lookup için."""
    norm = normalize_title(title)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


# In-memory recent-title cache (son 500 haber): Supabase sorgu sayısını düşürür.
_recent_title_hashes: "dict[str, datetime]" = {}
_RECENT_CACHE_MAX = 500
_RECENT_CACHE_TTL_HOURS = 48


def _as_utc(dt: datetime) -> datetime:
    """Naive datetime → UTC-aware'e çevirir. Zaten aware ise dokunmaz."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _prune_recent_cache() -> None:
    """TTL'ı geçmiş entry'leri ve max boyut aşıldıysa en eskileri at."""
    now = datetime.now(timezone.utc)
    ttl = timedelta(hours=_RECENT_CACHE_TTL_HOURS)
    expired = [h for h, ts in _recent_title_hashes.items() if now - _as_utc(ts) > ttl]
    for h in expired:
        _recent_title_hashes.pop(h, None)
    if len(_recent_title_hashes) > _RECENT_CACHE_MAX:
        # En eskilerden 100 tanesini at
        sorted_items = sorted(_recent_title_hashes.items(), key=lambda kv: _as_utc(kv[1]))
        for h, _ in sorted_items[:100]:
            _recent_title_hashes.pop(h, None)


async def _load_recent_title_hashes() -> None:
    """
    Startup'ta DB'den son 500 haberin başlığını çekip cache'i hydrate eder.
    Supervisor restart'larında memory'yi yeniden kurar.
    """
    try:
        async with AsyncSessionLocal() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=_RECENT_CACHE_TTL_HOURS)
            result = await session.execute(
                select(NewsItem.original_title, NewsItem.created_at)
                .where(NewsItem.created_at >= cutoff)
                .order_by(NewsItem.created_at.desc())
                .limit(_RECENT_CACHE_MAX)
            )
            rows = result.all()
        for title, created in rows:
            h = title_hash(title or "")
            if h:
                _recent_title_hashes[h] = _as_utc(created) if created else datetime.now(timezone.utc)
        logger.info(f"🗂️  Recent title cache hydrated: {len(_recent_title_hashes)} hash")
    except Exception as e:
        logger.warning(f"Recent title cache hydrate hatası: {e}")


def haber_kullaniciya_uygun(
    title: str,
    user_tags: str,
    custom_follows: str = "",
    body: str = "",
    symbol: str = "",
    category: str = "",
) -> bool:
    """
    Haber kullanıcının tag veya takip listesiyle eşleşiyor mu?

    v3.1: Arama yüzeyi genişletildi. Eskiden sadece başlıkta arıyorduk,
    ama FMP'den gelen US stock haberlerinin başlıkları çoğunlukla
    "BTQ Appoints Dr..." gibi tag keyword'lerine denk gelmiyor. Artık:
      • Title (eskisi gibi)
      • Body (haberin ilk 800 karakteri, FMP'den)
      • Symbol (FMP'nin atadığı ör: "AAPL", "BTCUSD")
      • Category (FMP endpoint etiketi: "crypto" → "Kripto" tag'i için)
    birlikte aranır.

    Eşleşme yoksa False → filtrelenir.
    """
    has_tags = bool(user_tags and user_tags.strip())
    has_follows = bool(custom_follows and custom_follows.strip())

    # İkisi de boşsa her haberi gönder
    if not has_tags and not has_follows:
        return True

    # Tüm arama yüzeyini tek bir lowercase haystack'te birleştir
    haystack_parts = [title or "", body or "", symbol or "", category or ""]
    haystack = " ".join(p for p in haystack_parts if p).lower()

    # FMP category → tag auto-match: FMP/crypto gelen haber otomatik "Kripto" tag'ine düşer
    category_tag_map = {
        "crypto": "kripto",
        "forex": "dolar",  # forex haberlerini Dolar/Euro tag'lerine yönlendir
        "stock": "hisse",
    }
    cat_lower = (category or "").lower()
    auto_tag = category_tag_map.get(cat_lower)

    if has_tags:
        for raw_tag in user_tags.split(","):
            tag = raw_tag.strip()
            if not tag:
                continue
            # Tag case-insensitive lookup: "hisse" / "HISSE" / "Hisse" hepsi bulsun
            tag_title = tag[:1].upper() + tag[1:].lower() if tag else tag
            keywords = (
                TAG_KEYWORDS.get(tag)
                or TAG_KEYWORDS.get(tag_title)
                or TAG_KEYWORDS.get(tag.upper())
                or [tag.lower()]
            )
            # Auto-tag matching (FMP/crypto → "Kripto")
            if auto_tag and auto_tag == tag.lower():
                return True
            if any(_keyword_in_haystack(kw, haystack) for kw in keywords):
                return True

    if has_follows:
        for keyword in [k.strip() for k in custom_follows.split(",") if k.strip()]:
            if _keyword_in_haystack(keyword, haystack):
                return True

    return False


def pick_emoji(title: str) -> str:
    title_lower = (title or "").lower()
    if any(w in title_lower for w in ["risk", "uyarı", "dikkat", "tehdit", "hack", "crash", "çöküş"]):
        return "⚠️"
    if any(w in title_lower for w in ["yüksel", "rally", "pump", "artış", "kazanç", "beat"]):
        return "🚀"
    if any(w in title_lower for w in ["dump", "plunge", "collapse", "sert", "hızlı", "flash"]):
        return "⚡"
    return "📊"


async def get_sources_from_db() -> dict:
    """Aktif RSS kaynaklarını DB'den çeker, yoksa default'a düşer."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Source).where(Source.is_active == True))
            sources = result.scalars().all()
        if sources:
            return {s.name: s.url for s in sources}
        logger.info("DB'de kaynak yok, varsayılan RSS kaynakları kullanılıyor.")
    except Exception as e:
        logger.error(f"DB kaynak çekme hatası: {e}")
    return DEFAULT_RSS_FEEDS


# ═══════════════════════════════════════════════════════════════════════════════
# AŞAMA 1: Fast Fetch Loop (60 sn) — FMP primary, RSS fallback, raw DB save
# ═══════════════════════════════════════════════════════════════════════════════

async def fast_fetch_once() -> int:
    """
    Tek bir fetch cycle: FMP (primary) + gerekirse RSS (fallback), dedup, DB'ye kaydet.
    Return: eklenen yeni haber sayısı.

    3 katmanlı deduplication:
      1) URL normalize (tracking params, fragment, trailing slash)
      2) Fuzzy title hash (aynı olayı farklı kaynaktan yazan duplicate'ları yakalar)
      3) DB-level UNIQUE(original_link) constraint (son güvenlik ağı)
    """
    _prune_recent_cache()

    all_news: List[dict] = []
    seen_urls: set = set()
    seen_title_hashes: set = set()
    dup_title_skipped = 0  # aynı başlık farklı URL → elendi

    # ═══ HİBRİT AKIŞ (v3.0) ═══════════════════════════════════════════════════
    # Eski sürüm: FMP → sessizse RSS (fallback).
    # Yeni sürüm: FMP (5 endpoint) + RSS (Türkçe+kripto) **her zaman paralel**.
    #
    # Mantık: FMP artık 500+ haberlik rotating pencere (stock/press/general/
    # crypto/forex) ile çalışıyor, BIST/TR kaynaklarını kapsamıyor. RSS bu kör
    # noktayı kapatmak için SÜREKLİ çalışır; fallback-only değil.
    #
    # AXIOM_DISABLE_RSS=1 → RSS'i tamamen kapatır (saf FMP moduna geçer).
    # ═══════════════════════════════════════════════════════════════════════════
    rss_disabled = os.getenv("AXIOM_DISABLE_RSS", "0").strip() in {"1", "true", "yes"}

    # Her iki kaynağı paralel başlat; birisi patlasa bile diğeri ilerlesin
    fmp_task = asyncio.create_task(fetch_fmp_news(limit=250))
    if not rss_disabled:
        sources = await get_sources_from_db()
        rss_task = asyncio.create_task(fetch_all_feeds(sources))
    else:
        rss_task = None

    # --- FMP'yi topla ---
    # fmp_fetched = toplam payload (5 endpoint'in toplamı)
    # fmp_new     = bu cycle'da aday kabul edilen (duplicate elenenler hariç)
    fmp_fetched = 0
    fmp_new = 0
    try:
        fmp_news = await fmp_task
        fmp_fetched = len(fmp_news)
        for item in fmp_news:
            raw_url = item.get("link", "")
            url = normalize_url(raw_url)
            title = item.get("title", "") or ""
            th = title_hash(title)

            if not url or url in seen_urls:
                continue
            if th and (th in seen_title_hashes or th in _recent_title_hashes):
                dup_title_skipped += 1
                continue

            item["link"] = url  # normalize edilmiş URL ile DB'ye yazılsın
            all_news.append(item)
            seen_urls.add(url)
            if th:
                seen_title_hashes.add(th)
            fmp_new += 1
    except Exception as e:
        logger.error(f"FMP fetch hatası: {e}")

    # --- RSS'i topla (paralel, hibrit kapsama) ---
    rss_count = 0
    if rss_task is not None:
        try:
            rss_news = await rss_task
            for item in rss_news:
                raw_url = item.get("link", "")
                url = normalize_url(raw_url)
                title = item.get("title", "") or ""
                th = title_hash(title)

                if not url or url in seen_urls:
                    continue
                if th and (th in seen_title_hashes or th in _recent_title_hashes):
                    dup_title_skipped += 1
                    continue

                item["link"] = url
                all_news.append(item)
                seen_urls.add(url)
                if th:
                    seen_title_hashes.add(th)
                rss_count += 1
        except Exception as e:
            logger.error(f"RSS fetch hatası: {e}")

    # Sağlık uyarısı: her iki kaynak da 0 döndürüyorsa bir şeyler yanlış
    if fmp_fetched == 0 and rss_count == 0 and not rss_disabled:
        logger.warning(
            "⚠️  HER İKİ KAYNAK SESSİZ: FMP payload=0 VE RSS new=0. "
            "API key / network / rate-limit problemi olabilir."
        )

    if not all_news:
        if dup_title_skipped:
            logger.info(
                f"⚡ FAST FETCH: 0 yeni haber "
                f"(FMP-payload:{fmp_fetched}, RSS-payload:{rss_count}, "
                f"title-dup eleme: {dup_title_skipped})"
            )
        return 0

    # --- DB'ye kaydet (analyzed=False olarak) ---
    # NOT: Eski haberleri DB'ye YAZIYORUZ (geriye dönük analiz için lazım);
    # dashboard gösterimindeki yaş filtresi feed endpoint'inde uygulanıyor.
    added = 0
    now = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as session:
            for item in all_news:
                link = item.get("link") or ""
                title = item.get("title") or "Başlık Yok"
                source = item.get("source") or "Bilinmeyen Kaynak"
                symbol = item.get("symbol")

                if not link or link == "#":
                    continue

                # DB'de var mı? (son güvenlik)
                exists = await session.execute(
                    select(NewsItem.id).where(NewsItem.original_link == link)
                )
                if exists.scalars().first():
                    continue

                urgent = is_urgent_title(title)
                body = (item.get("body") or "")[:2000]  # DB'de çok büyümesin
                try:
                    session.add(NewsItem(
                        source=source,
                        original_title=title,
                        original_link=link,
                        symbol=symbol,
                        body=body or None,
                        analyzed=False,
                        is_urgent=urgent,
                    ))
                    await session.commit()
                    added += 1
                    th = title_hash(title)
                    if th:
                        _recent_title_hashes[th] = now
                except IntegrityError:
                    await session.rollback()
                    continue
    except Exception as e:
        logger.error(f"Fast fetch DB yazım hatası: {e}")

    logger.info(
        f"⚡ FAST FETCH: {added} yeni haber "
        f"(FMP payload:{fmp_fetched} → yeni:{fmp_new}, RSS yeni:{rss_count}, "
        f"title-dup eleme: {dup_title_skipped})"
    )
    return added


async def fast_fetch_loop() -> None:
    """
    FAST_FETCH_INTERVAL_SEC saniyede bir FMP+RSS fetch çalıştırır.
    pipeline_health güncellenir; ardışık hatalarda exp. backoff.
    """
    logger.info(f"🟢 FAST FETCH LOOP başlatıldı (her {FAST_FETCH_INTERVAL_SEC}s)")
    import time as _t

    while True:
        ph = pipeline_health["fast_fetch"]
        try:
            count = await fast_fetch_once()
            ph["last_run_ts"] = _t.time()
            ph["last_new_count"] = count
            ph["total_runs"] += 1
            ph["consecutive_errors"] = 0
            sleep_sec = FAST_FETCH_INTERVAL_SEC
        except Exception as e:
            ph["total_errors"] += 1
            ph["consecutive_errors"] += 1
            logger.error(
                f"fast_fetch_once çöktü (consecutive: {ph['consecutive_errors']}): {e}",
                exc_info=True,
            )
            if ph["consecutive_errors"] >= 5:
                sleep_sec = min(FAST_FETCH_INTERVAL_SEC * 3, 180)
            else:
                sleep_sec = FAST_FETCH_INTERVAL_SEC
        await asyncio.sleep(sleep_sec)


# ═══════════════════════════════════════════════════════════════════════════════
# AŞAMA 2: Batch Analyze Loop (5 dk) — 10'ar haberi tek Gemini çağrısıyla analiz
# ═══════════════════════════════════════════════════════════════════════════════

# In-memory digest queue: analyzed ama non-urgent haberler burada bekler, digest_loop topluyor.
_digest_queue: asyncio.Queue = asyncio.Queue(maxsize=500)


async def _enrich_context(symbol: Optional[str]) -> Optional[dict]:
    """FMP'den şirket snapshot'ı çek (timeout'a düşebilir, None dönebilir)."""
    if not symbol:
        return None
    try:
        return await asyncio.wait_for(fetch_stock_snapshot(symbol), timeout=6.0)
    except Exception:
        return None


async def batch_analyze_once() -> int:
    """
    analyzed=False haberlerden BATCH_SIZE kadarını al, tek Gemini çağrısıyla analiz et,
    DB'yi güncelle, SSE'ye yayınla, urgent olanları anında Telegram'a at.
    Return: analiz edilen haber sayısı.
    """
    # 1) Analiz bekleyenleri al — YENİ haberler ÖNCE (DESC).
    # Neden: Fast-fetch her saniye yeni FMP/RSS haberleri çekiyor; tempo
    # batch analizden hızlı olunca kuyruğun arkasındaki eski haberler
    # kullanıcıya değersiz (5h eski forex manşeti kimseyi ilgilendirmez),
    # ama ön tarafta yeni breaking haberi anında analiz etmek kritik.
    # Eski ASC tercihi breaking haberi 1-2 saat geciktiriyordu.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NewsItem)
            .where(NewsItem.analyzed == False)  # noqa: E712
            .order_by(NewsItem.created_at.desc())
            .limit(BATCH_SIZE)
        )
        items = list(result.scalars().all())

    if not items:
        return 0

    logger.info(f"🧠 BATCH ANALYZE: {len(items)} haber analiz ediliyor...")

    # 2) Sembol varsa FMP context çek (paralel)
    contexts: Dict[int, Optional[dict]] = {}
    ctx_tasks = {item.id: asyncio.create_task(_enrich_context(item.symbol)) for item in items}
    for nid, task in ctx_tasks.items():
        try:
            contexts[nid] = await task
        except Exception:
            contexts[nid] = None

    # 3) Batch prompt hazırla — body varsa AI'a ilet (jenerik analiz engeli)
    batch_input = [
        {
            "id": item.id,
            "title": item.original_title,
            "link": item.original_link,
            "body": (item.body or "")[:800] if item.body else "",
            "context": contexts.get(item.id),
        }
        for item in items
    ]

    # 4) Tek Gemini çağrısı
    try:
        analyses = await generate_batch_summaries(batch_input)
    except Exception as e:
        logger.error(f"Batch Gemini çağrısı çöktü: {e}")
        return 0

    if not analyses:
        logger.warning("Batch Gemini boş dict döndü, bu cycle atlanıyor (next cycle'da retry).")
        return 0

    # 5) DB güncelle + SSE publish + broadcast routing
    updated = 0
    async with AsyncSessionLocal() as session:
        for item in items:
            analysis = analyses.get(item.id)
            if not analysis:
                # Bu haberi bu cycle'da Gemini atladı — sonraki cycle'da tekrar denenir.
                logger.debug(f"  ⏳ #{item.id} analiz edilmedi, next cycle'a kaldı")
                continue

            tg_hook = analysis.get("telegram_hook", "")
            dash = analysis.get("dashboard_summary", "")
            axiom = analysis.get("axiom_analysis", "")

            await session.execute(
                update(NewsItem)
                .where(NewsItem.id == item.id)
                .values(
                    telegram_hook=tg_hook,
                    dashboard_summary=dash,
                    axiom_analysis=axiom,
                    ai_summary=dash,  # legacy column compat
                    analyzed=True,
                )
            )
            await session.commit()
            updated += 1

            # 5a) SSE'ye yayınla — dashboard anında görsün
            # TAZELIK GATE: batch analyze DESC'de 15 item alıyor, ama yeni
            # haber tempo'su her zaman 15'i doldurmuyor → backlog'dan eski
            # item'lar SSE'ye düşüp dashboard'da yaş karışıklığı yaratıyor.
            # Sadece SSE_MAX_AGE_MIN içinde created_at'a sahip item'ları yayınla.
            is_fresh = True
            if SSE_MAX_AGE_MIN > 0 and item.created_at:
                age_min = (datetime.now(timezone.utc) - _as_utc(item.created_at)).total_seconds() / 60
                is_fresh = age_min <= SSE_MAX_AGE_MIN

            if is_fresh:
                try:
                    await news_bus.publish("news", {
                        "id": item.id,
                        "title": item.original_title,
                        "link": item.original_link,
                        "source": item.source,
                        "symbol": item.symbol,
                        "is_urgent": item.is_urgent,
                        "telegram_hook": tg_hook,
                        "dashboard_summary": dash,
                        "axiom_analysis": axiom,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    })
                except Exception as e:
                    logger.warning(f"SSE publish hatası #{item.id}: {e}")
            else:
                logger.debug(
                    f"  📭 SSE atlandı #{item.id} (yaş {age_min:.1f}dk > "
                    f"{SSE_MAX_AGE_MIN}dk gate); DB'de mevcut, feed reload'da görünür"
                )

            # 5b) Broadcast routing
            if item.is_urgent:
                # URGENT → anında Telegram'a
                logger.info(f"  🚨 URGENT broadcast: #{item.id} '{item.original_title[:50]}...'")
                try:
                    await broadcast_to_telegram(
                        item.id, item.original_title, tg_hook, item.source, item.original_link
                    )
                except Exception as e:
                    logger.error(f"Urgent broadcast hatası #{item.id}: {e}")
            else:
                # NORMAL → digest kuyruğuna
                try:
                    _digest_queue.put_nowait({
                        "id": item.id,
                        "title": item.original_title,
                        "hook": tg_hook,
                        "source": item.source,
                        "link": item.original_link,
                    })
                except asyncio.QueueFull:
                    logger.warning("Digest queue full, en eski item düşürülüyor")
                    try:
                        _digest_queue.get_nowait()
                        _digest_queue.put_nowait({
                            "id": item.id,
                            "title": item.original_title,
                            "hook": tg_hook,
                            "source": item.source,
                            "link": item.original_link,
                        })
                    except Exception:
                        pass

    logger.info(f"✅ BATCH ANALYZE: {updated}/{len(items)} güncellendi")
    return updated


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE HEALTH METRICS — /api/v1/health endpoint tarafından okunur
# ─────────────────────────────────────────────────────────────────────────────
pipeline_health: Dict[str, Any] = {
    "fast_fetch": {
        "last_run_ts": 0.0,
        "last_new_count": 0,
        "total_runs": 0,
        "total_errors": 0,
        "consecutive_errors": 0,
    },
    "batch_analyze": {
        "last_run_ts": 0.0,
        "last_analyzed_count": 0,
        "total_analyzed": 0,
        "total_errors": 0,
        "consecutive_errors": 0,
    },
    "digest": {
        "last_run_ts": 0.0,
        "last_sent_count": 0,
        "total_runs": 0,
    },
}


async def batch_analyze_loop() -> None:
    """
    batch_analyze_once'u sonsuz döngüde çalıştırır.

    Dayanıklılık:
      - Her cycle kendi try/except'i içinde. Exception loop'u durdurmaz.
      - Consecutive error tracking: ardışık N hata sonrası exponential backoff
        (60s → 120s → 240s max) — DB down / Gemini down gibi durumlarda.
      - pipeline_health global dict güncellenir (monitoring için).
    """
    logger.info(
        f"🟢 BATCH ANALYZE LOOP başlatıldı (her {BATCH_ANALYZE_INTERVAL_SEC}s, "
        f"{BATCH_SIZE} haber/cycle)"
    )
    ph = pipeline_health["batch_analyze"]

    import time as _t

    while True:
        try:
            count = await batch_analyze_once()
            ph["last_run_ts"] = _t.time()
            ph["last_analyzed_count"] = count
            ph["total_analyzed"] += count
            ph["consecutive_errors"] = 0  # başarı → counter sıfırla
            sleep_sec = BATCH_ANALYZE_INTERVAL_SEC
        except Exception as e:
            ph["total_errors"] += 1
            ph["consecutive_errors"] += 1
            logger.error(
                f"batch_analyze_once çöktü (consecutive: {ph['consecutive_errors']}): {e}",
                exc_info=True,
            )
            # Exponential backoff on consecutive failures — DB down / Gemini down
            # gibi durumlarda spam log ve gereksiz yük önlenir.
            # 1-3: normal interval, 4-6: 2x, 7+: 4x max
            if ph["consecutive_errors"] >= 7:
                sleep_sec = min(BATCH_ANALYZE_INTERVAL_SEC * 4, 240)
            elif ph["consecutive_errors"] >= 4:
                sleep_sec = BATCH_ANALYZE_INTERVAL_SEC * 2
            else:
                sleep_sec = BATCH_ANALYZE_INTERVAL_SEC

        await asyncio.sleep(sleep_sec)


# ═══════════════════════════════════════════════════════════════════════════════
# AŞAMA 3: Telegram Broadcasting (urgent instant + digest batched)
# ═══════════════════════════════════════════════════════════════════════════════

async def broadcast_to_telegram(
    news_id: int,
    title: str,
    hook: str,
    source: str,
    link: str,
    body: str = "",
    symbol: str = "",
    category: str = "",
) -> None:
    """
    Tek bir haberi tag/follow filtresi ile uygun kullanıcılara iletir.
    Idempotent: broadcast_at set edilmişse tekrar göndermez.

    body/symbol/category: filtre arama yüzeyini genişletir (v3.1).
    Caller geçmezse DB'den doldurulur (geri uyumluluk).
    """
    # Idempotency check + eksik alanları DB'den tamamla
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            select(NewsItem.broadcast_at, NewsItem.body, NewsItem.symbol, NewsItem.source)
            .where(NewsItem.id == news_id)
        )
        r = row.first()
        if r is None:
            return
        existing = r[0]
        if existing is not None:
            logger.debug(f"⏭️  #{news_id} zaten broadcast edildi, atlanıyor")
            return
        # Caller boş bıraktıysa DB'den al
        if not body:
            body = r[1] or ""
        if not symbol:
            symbol = r[2] or ""
        # Category'i source etiketinden çıkar ("Benzinga · FMP/stock" → "stock")
        if not category:
            src = r[3] or ""
            if "FMP/" in src:
                category = src.split("FMP/", 1)[1].strip()

    # Kullanıcıları çek
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User))
            users = list(result.scalars().all())
    except Exception as e:
        logger.error(f"Broadcast: kullanıcı listesi çekilemedi: {e}")
        return

    hook = hook or "⚠️ Özet alınamadı"
    source = source or "Bilinmeyen Kaynak"
    safe_link = html.escape(link or "#", quote=True)
    emoji = pick_emoji(title)

    base_message = (
        f"{emoji} <b>{html.escape(title)}</b>\n\n"
        f"{hook}\n\n"
        f"🔗 <a href='{safe_link}'>Orijinal Kaynak</a> • <b>{html.escape(source)}</b>"
    )

    sent = 0
    skipped = 0
    for user in users:
        user_tags = user.tags if user.tags is not None else ""
        user_follows = user.custom_follows if user.custom_follows is not None else ""
        if not haber_kullaniciya_uygun(title, user_tags, user_follows, body=body, symbol=symbol, category=category):
            skipped += 1
            logger.debug(
                f"  ⏭️  skip user={user.telegram_id} title='{title[:50]}...' "
                f"tags='{user_tags}' follows='{user_follows}'"
            )
            continue

        has_prefs = bool(user_tags.strip()) or bool(user_follows.strip())
        cta = random.choice(CTA_TAG_PROMPTS if not has_prefs else CTA_DASHBOARD_PROMPTS)
        full_message = base_message + cta

        try:
            await asyncio.to_thread(send_telegram_message, user.telegram_id, full_message)
            sent += 1
            logger.info(f"  ✅ sent user={user.telegram_id} news=#{news_id}")
        except Exception as e:
            if "chat not found" not in str(e).lower():
                logger.warning(f"  ✗ {user.telegram_id}: {e}")

    # broadcast_at damgası vur — idempotent
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(NewsItem)
                .where(NewsItem.id == news_id)
                .values(broadcast_at=datetime.now(timezone.utc))
            )
            await session.commit()
    except Exception as e:
        logger.warning(f"broadcast_at yazılamadı #{news_id}: {e}")

    logger.info(f"📣 Broadcast #{news_id}: {sent} gönderildi / {skipped} filtrelendi / {len(users)} toplam")


async def digest_loop() -> None:
    """
    15 dakikada bir digest queue'yu boşaltır, tek bir özet mesajı olarak gönderir.
    Urgent değil ama normalde haberi kaçırmasın diye gruplu atış.
    """
    logger.info(f"🟢 DIGEST LOOP başlatıldı (her {DIGEST_INTERVAL_SEC}s)")
    while True:
        await asyncio.sleep(DIGEST_INTERVAL_SEC)

        batch: List[dict] = []
        while True:
            try:
                batch.append(_digest_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            logger.debug("Digest: boş kuyruk, atlanıyor")
            continue

        logger.info(f"📬 DIGEST: {len(batch)} haber toplu gönderiliyor")
        # Her haberi kendi broadcast'ını almış gibi sırayla gönder.
        # (Basit yaklaşım — gelecekte tek mesaj özetleyici eklenebilir.)
        for item in batch:
            try:
                await broadcast_to_telegram(
                    item["id"], item["title"], item["hook"], item["source"], item["link"]
                )
            except Exception as e:
                logger.error(f"Digest broadcast hatası #{item.get('id')}: {e}")
            await asyncio.sleep(1.5)  # Telegram rate limit koruması


# ═══════════════════════════════════════════════════════════════════════════════
# Ana entrypoint (main.py lifespan bunu çağırır)
# ═══════════════════════════════════════════════════════════════════════════════

async def run_crawler() -> None:
    """
    Üç bağımsız döngüyü paralelde çalıştırır. Herhangi biri çökerse diğerleri
    etkilenmez; main.py'daki supervisor tüm run_crawler()'ı restart eder.
    """
    logger.info("🚀 Axiom Haber Motoru v3.0 ayağa kalkıyor (FMP 5-endpoint paralel + RSS hibrit TR/kripto)")
    # Son 48 saatteki başlık hash cache'i → fuzzy dedup için hot start
    await _load_recent_title_hashes()
    await asyncio.gather(
        fast_fetch_loop(),
        batch_analyze_loop(),
        digest_loop(),
    )
