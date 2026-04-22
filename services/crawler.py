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
from typing import Optional, List, Dict, Tuple

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
FAST_FETCH_INTERVAL_SEC = int(os.getenv("AXIOM_FAST_FETCH_SEC", "30"))  # 30 sn
BATCH_ANALYZE_INTERVAL_SEC = int(os.getenv("AXIOM_BATCH_ANALYZE_SEC", "90"))  # 1.5 dk
DIGEST_INTERVAL_SEC = int(os.getenv("AXIOM_DIGEST_SEC", "180"))  # 3 dk
BATCH_SIZE = int(os.getenv("AXIOM_BATCH_SIZE", "8"))  # Gemini çağrı başına haber adedi (429'u düşürür)
FMP_FALLBACK_THRESHOLD = int(os.getenv("AXIOM_FMP_FALLBACK_MIN", "5"))  # FMP bundan az dönerse RSS ekle

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
    "BTC":    ["btc", "bitcoin", "satoshi", "lightning network", "halving"],
    "Altın":  ["altın", "altin", "gold", "xau", "altın ons", "ons altın", "gram altın"],
    "BIST":   [
        "bist", "borsa istanbul", "bist 100", "bist30", "thyao", "garan", "akbnk",
        "eregl", "aselsan", "asels", "kchol", "sahol", "tuprs", "sise", "toasō",
        "froto", "pgsus", "kozal", "enjsa", "yapı kredi", "ykbnk",
    ],
    "Dolar":  ["dolar", "dollar", "usd", "dxy", "greenback", "usd/try"],
    "Faiz":   [
        "faiz", "interest rate", "fed funds", "tcmb", "rate hike", "rate cut",
        "rate decision", "policy rate", "benchmark rate", "ecb rate",
    ],
    "Fed":    [
        "fed", "fomc", "powell", "federal reserve", "jerome powell",
        "fed meeting", "fed decision", "quantitative easing", "taper",
    ],
    "Euro":   ["euro", "eur", "eur/usd", "ecb", "european central bank"],
    "Petrol": [
        "petrol", "oil", "brent", "wti", "opec", "crude", "barrel", "pipeline",
        "exxon", "chevron", "shell", "bp", "oil price", "refinery",
    ],
    "Kripto": [
        "kripto", "crypto", "ethereum", "eth", "solana", "sol", "blockchain",
        "defi", "nft", "altcoin", "stablecoin", "usdt", "usdc", "binance",
        "coinbase", "ripple", "xrp", "cardano", "ada", "dogecoin", "doge",
        "avalanche", "avax", "polygon", "matic", "chainlink", "link",
        "token", "web3", "layer 2", "bridge",
    ],
    "Hisse":  [
        "hisse", "stock", "stocks", "equity", "equities", "shares", "share price",
        "s&p", "s&p 500", "nasdaq", "dow jones", "dow", "russell", "ftse", "dax",
        "earnings", "guidance", "revenue", "ceo", "cfo", "ipo", "buyback",
        "dividend", "merger", "acquisition", "spin-off", "wall street",
        "market cap", "analyst", "upgrade", "downgrade", "price target",
        "apple", "microsoft", "google", "alphabet", "amazon", "meta", "tesla",
        "nvidia", "aapl", "msft", "goog", "googl", "amzn", "meta", "tsla", "nvda",
        "form 8k", "form 13f", "form 10k", "form 10q",  # SEC filings = equity
    ],
}


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


def _prune_recent_cache() -> None:
    """TTL'ı geçmiş entry'leri ve max boyut aşıldıysa en eskileri at."""
    now = datetime.now(timezone.utc)
    ttl = timedelta(hours=_RECENT_CACHE_TTL_HOURS)
    expired = [h for h, ts in _recent_title_hashes.items() if now - ts > ttl]
    for h in expired:
        _recent_title_hashes.pop(h, None)
    if len(_recent_title_hashes) > _RECENT_CACHE_MAX:
        # En eskilerden 100 tanesini at
        sorted_items = sorted(_recent_title_hashes.items(), key=lambda kv: kv[1])
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
                _recent_title_hashes[h] = created or datetime.now(timezone.utc)
        logger.info(f"🗂️  Recent title cache hydrated: {len(_recent_title_hashes)} hash")
    except Exception as e:
        logger.warning(f"Recent title cache hydrate hatası: {e}")


def haber_kullaniciya_uygun(title: str, user_tags: str, custom_follows: str = "") -> bool:
    """
    Haber başlığı kullanıcının tag veya takip listesiyle eşleşiyor mu?

    Fuzzy-ish: case-insensitive, tag case-normalize (hissE → Hisse), sembol
    word-boundary'siz substring match. Eşleşme yoksa False → filtrelenir.
    """
    has_tags = bool(user_tags and user_tags.strip())
    has_follows = bool(custom_follows and custom_follows.strip())

    # İkisi de boşsa her haberi gönder
    if not has_tags and not has_follows:
        return True

    title_lower = title.lower()

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
            if any(kw in title_lower for kw in keywords):
                return True

    if has_follows:
        for keyword in [k.strip() for k in custom_follows.split(",") if k.strip()]:
            if keyword.lower() in title_lower:
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

    # --- FMP (primary) ---
    # İki ayrı sayaç:
    #   fmp_fetched  = FMP API'den gelen toplam payload (dedup'tan bağımsız)
    #   fmp_new      = bu cycle'da DB'ye aday olarak eklenen (duplicate olmayan)
    # Fallback kararı fmp_new'e göre DEĞİL fmp_fetched'e göre verilir; çünkü FMP
    # 50 haber çekip hepsi son 48 saatte görülmüşse bu "kaynak sessiz" değil,
    # "yeni olay yok" demektir. RSS'e düşmek dashboard'ı eski haberle kirletir.
    fmp_fetched = 0
    fmp_new = 0
    try:
        fmp_news = await fetch_fmp_news(limit=50)
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

    # --- RSS (fallback: SADECE FMP tamamen sessizse) ---
    # Not: Kullanıcı isterse AXIOM_DISABLE_RSS=1 ile RSS'i tamamen kapatabilir.
    rss_count = 0
    rss_disabled = os.getenv("AXIOM_DISABLE_RSS", "0").strip() in {"1", "true", "yes"}
    need_rss = (fmp_fetched == 0) and not rss_disabled
    if need_rss:
        logger.warning(
            f"⚠️  FMP payload=0 (API sessiz/hatalı) — RSS fallback devreye giriyor. "
            f"Detay için yukarıda 'FMP HTTP' logu bakılmalı."
        )
    else:
        # FMP sağlıklı döndü (fmp_fetched>0); bu cycle'da dedup tamamladıysa yeni
        # haber yoksa bile bu normal (FMP yenilenene kadar bekleriz). RSS'e geçmiyoruz.
        logger.debug(
            f"FMP sağlıklı (payload={fmp_fetched}, new={fmp_new}); RSS atlandı"
        )

    if need_rss:
        try:
            sources = await get_sources_from_db()
            rss_news = await fetch_all_feeds(sources)
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

    if not all_news:
        if dup_title_skipped:
            logger.info(
                f"⚡ FAST FETCH: 0 yeni haber "
                f"(FMP-payload:{fmp_fetched}, title-dup eleme: {dup_title_skipped})"
            )
        return 0

    # --- DB'ye kaydet (analyzed=False olarak) ---
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
    """60 saniyede bir FMP+RSS fetch çalıştırır. Çöktüğünde supervisor restart eder."""
    logger.info(f"🟢 FAST FETCH LOOP başlatıldı (her {FAST_FETCH_INTERVAL_SEC}s)")
    while True:
        try:
            await fast_fetch_once()
        except Exception as e:
            logger.error(f"fast_fetch_once çöktü: {e}", exc_info=True)
        await asyncio.sleep(FAST_FETCH_INTERVAL_SEC)


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
    # 1) Analiz bekleyenleri al
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(NewsItem)
            .where(NewsItem.analyzed == False)  # noqa: E712
            .order_by(NewsItem.created_at.asc())
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


async def batch_analyze_loop() -> None:
    logger.info(f"🟢 BATCH ANALYZE LOOP başlatıldı (her {BATCH_ANALYZE_INTERVAL_SEC}s, {BATCH_SIZE} haber/cycle)")
    while True:
        try:
            await batch_analyze_once()
        except Exception as e:
            logger.error(f"batch_analyze_once çöktü: {e}", exc_info=True)
        await asyncio.sleep(BATCH_ANALYZE_INTERVAL_SEC)


# ═══════════════════════════════════════════════════════════════════════════════
# AŞAMA 3: Telegram Broadcasting (urgent instant + digest batched)
# ═══════════════════════════════════════════════════════════════════════════════

async def broadcast_to_telegram(
    news_id: int, title: str, hook: str, source: str, link: str
) -> None:
    """
    Tek bir haberi tag/follow filtresi ile uygun kullanıcılara iletir.
    Idempotent: broadcast_at set edilmişse tekrar göndermez.
    """
    # Idempotency check
    async with AsyncSessionLocal() as session:
        row = await session.execute(
            select(NewsItem.broadcast_at).where(NewsItem.id == news_id)
        )
        existing = row.scalars().first()
        if existing is not None:
            logger.debug(f"⏭️  #{news_id} zaten broadcast edildi, atlanıyor")
            return

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
        if not haber_kullaniciya_uygun(title, user_tags, user_follows):
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
    logger.info("🚀 Axiom Haber Motoru v2.1 ayağa kalkıyor (FMP-primary + payload-aware fallback)")
    # Son 48 saatteki başlık hash cache'i → fuzzy dedup için hot start
    await _load_recent_title_hashes()
    await asyncio.gather(
        fast_fetch_loop(),
        batch_analyze_loop(),
        digest_loop(),
    )
