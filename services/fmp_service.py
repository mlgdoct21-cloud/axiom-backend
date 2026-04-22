"""
FMP (Financial Modeling Prep) servisi — haber ve stock snapshot çekme.

HİBRİT MİMARİ (v3.0)
────────────────────
Eski sürüm sadece `news/stock-latest` (50 haber) sorguluyordu — FMP aynı
50 haberlik pencereyi döndürdüğü için 15 sn'lik polling'de yeni haber
yakalama olasılığı düşüktü ("3h-eski-haber" problemi).

Yeni sürüm **5 FMP endpoint'ini paralel** sorgular:
  • news/stock-latest          → hisse haberleri        (limit 250)
  • news/press-releases-latest → kurumsal basın bültenleri (100)
  • news/general-latest        → genel finans haberleri (100)
  • news/crypto-latest         → kripto haberleri       (100)
  • news/forex-latest          → forex haberleri        (50)

Sonuç: yaklaşık **600 haberlik rotating pencere** → yeni haber yakalama
olasılığı 5-10x artar. Turkish BIST ve crypto-native gaps ise rss_service'te.
"""
import os
import asyncio
import aiohttp
from core.logger import get_logger

logger = get_logger("fmp_service")

# FMP API Base URL
FMP_BASE_URL = "https://financialmodelingprep.com/stable"

# (endpoint_path, default_limit, category_label)
# Category, crawler ve dashboard tarafında hangi endpoint'ten geldiğini
# ayırt etmek için kaynak etiketine eklenir: "Benzinga · FMP/stock"
FMP_ENDPOINTS = [
    ("news/stock-latest",           250, "stock"),
    ("news/press-releases-latest",  100, "press"),
    ("news/general-latest",         100, "general"),
    ("news/crypto-latest",          100, "crypto"),
    ("news/forex-latest",            50, "forex"),
]


async def _fetch_one_endpoint(
    session: aiohttp.ClientSession,
    endpoint: str,
    limit: int,
    category: str,
    api_key: str,
) -> list:
    """
    Tek bir FMP endpoint'ini çeker. Hata/timeout durumunda **boş liste** döner;
    bir endpoint'in çökmesi diğerlerini etkilemez (asyncio.gather + isolated).
    """
    url = f"{FMP_BASE_URL}/{endpoint}?page=0&limit={limit}&apikey={api_key}"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=12)
        ) as response:
            if response.status != 200:
                try:
                    body_preview = (await response.text())[:200]
                except Exception:
                    body_preview = "<read-failed>"
                logger.warning(
                    f"FMP[{category}] HTTP {response.status} → endpoint atlandı. "
                    f"Body: {body_preview}"
                )
                return []

            data = await response.json()
            if not isinstance(data, list):
                logger.warning(
                    f"FMP[{category}] beklenmeyen payload tipi: "
                    f"{type(data).__name__}"
                )
                return []

            formatted = []
            for item in data:
                # Haber gövdesi: AI'nin sadece başlığa bakıp jenerik özet
                # üretmesini engellemek için ilk 800 karakteri iletiyoruz.
                body = (item.get("text") or "").strip()
                if len(body) > 800:
                    body = body[:800] + "…"

                # Kaynak etiketi: orijinal site + FMP kategorisi
                # Örn: "Benzinga · FMP/stock", "CoinDesk · FMP/crypto"
                original_site = (item.get("site") or "FMP").strip() or "FMP"
                source_label = f"{original_site} · FMP/{category}"

                formatted.append({
                    "title": item.get("title", "Başlık Yok"),
                    "link": item.get("url", "#"),
                    "source": source_label,
                    "symbol": item.get("symbol"),
                    "body": body,
                    "category": category,
                    "raw_fmp_data": item,
                })
            logger.info(f"📡 FMP[{category}]: {len(formatted)} haber çekildi")
            return formatted
    except asyncio.TimeoutError:
        logger.warning(f"FMP[{category}] timeout (12s) — endpoint atlandı")
        return []
    except Exception as e:
        logger.warning(f"FMP[{category}] hatası: {e}")
        return []


async def fetch_fmp_news(limit: int = 250) -> list:
    """
    5 FMP endpoint'inden paralel olarak haber çeker (coverage 5x).

    `limit` parametresi sadece stock-latest için kullanılır (geriye dönük
    uyumluluk); press/general/crypto/forex için FMP_ENDPOINTS'teki sabit
    değerler geçerlidir.

    Returns: {title, link, source, symbol, body, category, raw_fmp_data}
    """
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        logger.error("FMP_API_KEY bulunamadı.")
        return []

    # stock-latest'e kullanıcı limit'ini aktar; diğerleri sabit
    endpoints_with_limits = [
        (ep, (limit if cat == "stock" else lim), cat)
        for ep, lim, cat in FMP_ENDPOINTS
    ]

    try:
        async with aiohttp.ClientSession() as session:
            tasks = [
                _fetch_one_endpoint(session, ep, lim, cat, api_key)
                for ep, lim, cat in endpoints_with_limits
            ]
            # return_exceptions=True: bir task patlasa bile diğerleri çalışsın
            results = await asyncio.gather(*tasks, return_exceptions=True)

            all_news = []
            breakdown = []
            for (ep, lim, cat), res in zip(endpoints_with_limits, results):
                if isinstance(res, Exception):
                    logger.warning(
                        f"FMP[{cat}] exception (ignored): {res}"
                    )
                    breakdown.append(f"{cat}:ERR")
                    continue
                all_news.extend(res)
                breakdown.append(f"{cat}:{len(res)}")

            logger.info(
                f"📡 FMP TOTAL: {len(all_news)} haber "
                f"({', '.join(breakdown)})"
            )
            return all_news
    except Exception as e:
        logger.error(f"FMP genel fetch hatası: {e}")
        return []


async def fetch_stock_snapshot(symbol: str) -> dict:
    """
    Hisse senedi hakkında temel finansal karne çeker (Profile + Key Metrics TTM).
    AI analizine context sağlamak için kullanılır.
    """
    if not symbol:
        return None
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        return None

    # Stable endpoints
    profile_url = (
        f"{FMP_BASE_URL}/profile?symbol={symbol}&apikey={api_key}"
    )
    metrics_url = (
        f"{FMP_BASE_URL}/key-metrics-ttm?symbol={symbol}&apikey={api_key}"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(profile_url, timeout=5) as p_res:
                profile = await p_res.json() if p_res.status == 200 else []

            async with session.get(metrics_url, timeout=5) as m_res:
                metrics = await m_res.json() if m_res.status == 200 else []

            p_data = profile[0] if profile and isinstance(profile, list) else {}
            m_data = metrics[0] if metrics and isinstance(metrics, list) else {}

            if not p_data:
                return None

            return {
                "name": p_data.get("companyName"),
                "sector": p_data.get("sector"),
                "industry": p_data.get("industry"),
                "description": p_data.get("description", "")[:500],
                "price": p_data.get("price"),
                "marketCap": p_data.get("marketCap"),
                "pe": m_data.get("peRatioTTM"),
                "roe": m_data.get("roeTTM"),
                "debtToEquity": m_data.get("debtToEquityTTM"),
                "currentRatio": m_data.get("currentRatioTTM"),
                "dividendYield": m_data.get("dividendYieldTTM"),
                "eps": m_data.get("netIncomePerShareTTM"),
            }
    except Exception as e:
        logger.error(f"Snapshot çekme hatası ({symbol}): {e}")
        return None
