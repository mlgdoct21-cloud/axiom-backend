import os
import aiohttp
from core.logger import get_logger

logger = get_logger("fmp_service")

# FMP API Base URL
FMP_BASE_URL = "https://financialmodelingprep.com/stable"

async def fetch_fmp_news(limit: int = 50) -> list:
    """
    Financial Modeling Prep (FMP) üzerinden gerçek zamanlı profesyonel haberleri çeker.
    """
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        logger.error("FMP_API_KEY bulunamadı.")
        return []

    url = f"{FMP_BASE_URL}/news/stock-latest?page=0&limit={limit}&apikey={api_key}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as response:
                if response.status != 200:
                    # Sessiz başarısızlığı kır — artık tam detayı logluyoruz
                    # (rate limit / unauthorized / invalid API key vb.)
                    try:
                        body_preview = (await response.text())[:200]
                    except Exception:
                        body_preview = "<read-failed>"
                    logger.error(
                        f"FMP HTTP {response.status} → fallback RSS devreye girecek. "
                        f"Body: {body_preview}"
                    )
                    return []
                data = await response.json()
                if not isinstance(data, list):
                    logger.error(f"FMP beklenmeyen payload tipi: {type(data).__name__}")
                    return []

                formatted_news = []
                for item in data:
                    # FMP news haberin gövdesini `text` alanında döner — AI'nin
                    # sadece başlığa bakıp jenerik özet üretmesini engellemek için
                    # bunu da iletiyoruz (ilk 800 karakter yeterli).
                    body = (item.get("text") or "").strip()
                    if len(body) > 800:
                        body = body[:800] + "…"
                    # Orijinal site (ör: "Investing.com", "Benzinga") + FMP etiketi.
                    # Bu sayede dashboard/Telegram'da hangi haberin FMP API'den,
                    # hangisinin direkt RSS feed'inden geldiği ayırt edilebilir.
                    original_site = (item.get("site") or "FMP").strip() or "FMP"
                    source_label = f"{original_site} · FMP"
                    formatted_news.append({
                        "title": item.get("title", "Başlık Yok"),
                        "link": item.get("url", "#"),
                        "source": source_label,
                        "symbol": item.get("symbol"),
                        "body": body,
                        "raw_fmp_data": item,
                    })
                logger.info(f"📡 FMP: {len(formatted_news)} haber çekildi")
                return formatted_news
    except Exception as e:
        logger.error(f"FMP Haber çekme hatası: {e}")
        return []

async def fetch_stock_snapshot(symbol: str) -> dict:
    """
    Hisse senedi hakkında temel finansal karne çeker (Profile + Key Metrics TTM).
    AI analizine context sağlamak için kullanılır.
    """
    if not symbol: return None
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key: return None

    # Stable endpoints
    profile_url = f"https://financialmodelingprep.com/stable/profile?symbol={symbol}&apikey={api_key}"
    metrics_url = f"https://financialmodelingprep.com/stable/key-metrics-ttm?symbol={symbol}&apikey={api_key}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(profile_url, timeout=5) as p_res:
                profile = await p_res.json() if p_res.status == 200 else []
            
            async with session.get(metrics_url, timeout=5) as m_res:
                metrics = await m_res.json() if m_res.status == 200 else []
            
            p_data = profile[0] if profile and isinstance(profile, list) else {}
            m_data = metrics[0] if metrics and isinstance(metrics, list) else {}
            
            if not p_data: return None

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
                "eps": m_data.get("netIncomePerShareTTM")
            }
    except Exception as e:
        logger.error(f"Snapshot çekme hatası ({symbol}): {e}")
        return None
