"""
Market Summary Service — Dashboard "Günün Özeti" 6 panel için FMP entegrasyonu.

PANELLER:
  1. Overnight Markets   → /quote (Asya/Avrupa/US futures endeksleri)
  2. ETF Flows           → BTC/ETH spot ETF günlük inflow (FMP /etf-holder + Coingecko)
  3. Economic Calendar   → /economic-calendar (high-impact bugün)
  4. Pre-Market Movers   → /stock_market/gainers + /losers (pre-market)
  5. Earnings Today      → /earning_calendar (bugün açıklanacaklar)
  6. Sector Performance  → /sector-performance (sektör heatmap)

Tüm panelleri paralel olarak (asyncio.gather) çekeriz; bir endpoint timeout'a
düşse bile diğerleri etkilenmez.
"""
import os
import asyncio
import aiohttp
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("market_summary_service")

FMP_BASE_URL = "https://financialmodelingprep.com/stable"
FMP_LEGACY_URL = "https://financialmodelingprep.com/api/v3"  # bazı endpoint'ler hâlâ v3

# Endeks sembolleri (FMP'nin kullandığı format)
OVERNIGHT_INDICES = {
    "asia": [
        ("^N225", "NIKKEI",   "🇯🇵"),
        ("^HSI", "HANG SENG", "🇭🇰"),
        ("000001.SS", "SHANGHAI", "🇨🇳"),
        ("^AXJO", "ASX 200", "🇦🇺"),
        ("^KS11", "KOSPI", "🇰🇷"),
    ],
    "europe": [
        ("^GDAXI", "DAX",  "🇩🇪"),
        ("^FTSE",  "FTSE 100", "🇬🇧"),
        ("^FCHI",  "CAC 40", "🇫🇷"),
        ("^STOXX50E", "STOXX 50", "🇪🇺"),
    ],
    "us_futures": [
        ("ES=F", "S&P 500 Fut", "📈"),
        ("NQ=F", "NASDAQ Fut", "📈"),
        ("YM=F", "DOW Fut", "📈"),
    ],
}


def _api_key() -> str:
    return os.getenv("FMP_API_KEY", "").strip()


async def _fmp_get(session: aiohttp.ClientSession, url: str, timeout: int = 8) -> Any:
    """Generic FMP GET — hata yutar, None döner."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                logger.warning(f"FMP GET {resp.status}: {url[:80]}")
                return None
            return await resp.json()
    except asyncio.TimeoutError:
        logger.warning(f"FMP GET timeout: {url[:80]}")
        return None
    except Exception as e:
        logger.warning(f"FMP GET error: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# 1. OVERNIGHT MARKETS
# ════════════════════════════════════════════════════════════════════════════

async def get_overnight_markets() -> Dict[str, Any]:
    """
    Asya, Avrupa endeksleri ve US futures — kullanıcı uyurken neler oldu?
    FMP /quote/{symbol1,symbol2,...} batch endpoint kullanır.
    """
    api_key = _api_key()
    if not api_key:
        logger.error("FMP_API_KEY missing — overnight markets skipped")
        return {"asia": [], "europe": [], "us_futures": [], "error": "no_api_key"}

    # Tüm sembolleri tek string'e topla (FMP batch quote)
    all_symbols = []
    for region_syms in OVERNIGHT_INDICES.values():
        all_symbols.extend([s[0] for s in region_syms])
    symbols_str = ",".join(all_symbols)

    url = f"{FMP_LEGACY_URL}/quote/{symbols_str}?apikey={api_key}"

    async with aiohttp.ClientSession() as session:
        data = await _fmp_get(session, url)

    if not data or not isinstance(data, list):
        return {"asia": [], "europe": [], "us_futures": [], "error": "no_data"}

    # Symbol → quote dict
    quote_map = {item.get("symbol"): item for item in data if isinstance(item, dict)}

    def _format_index(symbol: str, label: str, flag: str) -> Optional[Dict[str, Any]]:
        q = quote_map.get(symbol)
        if not q:
            return None
        change_pct = q.get("changesPercentage") or 0
        return {
            "symbol": symbol,
            "label": label,
            "flag": flag,
            "price": round(q.get("price") or 0, 2),
            "change": round(q.get("change") or 0, 2),
            "change_pct": round(change_pct, 2),
            "trend": "up" if change_pct >= 0 else "down",
        }

    result = {}
    for region, syms in OVERNIGHT_INDICES.items():
        region_data = []
        for symbol, label, flag in syms:
            formatted = _format_index(symbol, label, flag)
            if formatted:
                region_data.append(formatted)
        result[region] = region_data

    logger.info(f"Overnight markets: asia={len(result['asia'])}, eu={len(result['europe'])}, us={len(result['us_futures'])}")
    return result


# ════════════════════════════════════════════════════════════════════════════
# 2. ETF FLOWS (BTC/ETH SPOT)
# ════════════════════════════════════════════════════════════════════════════

# Bilinen Bitcoin Spot ETF'leri (US-listed)
BTC_SPOT_ETFS = ["IBIT", "FBTC", "BITB", "ARKB", "BTCO", "EZBC", "BRRR", "HODL"]
ETH_SPOT_ETFS = ["ETHA", "FETH", "ETHV", "ETHW", "QETH", "EZET"]


async def _fetch_etf_quote(session: aiohttp.ClientSession, symbol: str, api_key: str) -> Optional[Dict[str, Any]]:
    """Tek bir ETF için quote + AUM çek."""
    url = f"{FMP_LEGACY_URL}/quote/{symbol}?apikey={api_key}"
    data = await _fmp_get(session, url, timeout=5)
    if not data or not isinstance(data, list) or not data:
        return None
    q = data[0]
    return {
        "symbol": symbol,
        "price": q.get("price"),
        "volume": q.get("volume") or 0,
        "avg_volume": q.get("avgVolume") or 0,
        "market_cap": q.get("marketCap") or 0,  # AUM proxy
        "change_pct": q.get("changesPercentage") or 0,
    }


async def get_etf_flows() -> Dict[str, Any]:
    """
    BTC ve ETH Spot ETF'leri için aggregated metrics.

    NOTE: FMP free tier daily flow datası vermiyor; biz volume × price
    proxy'sini "estimated daily inflow" olarak veriyoruz. Tam flow datası
    için FarSide Investors scraping (ileride) gerekir.
    """
    api_key = _api_key()
    if not api_key:
        return {"btc": {}, "eth": {}, "error": "no_api_key"}

    async with aiohttp.ClientSession() as session:
        btc_tasks = [_fetch_etf_quote(session, s, api_key) for s in BTC_SPOT_ETFS]
        eth_tasks = [_fetch_etf_quote(session, s, api_key) for s in ETH_SPOT_ETFS]
        all_results = await asyncio.gather(*btc_tasks, *eth_tasks, return_exceptions=True)

    btc_results = [r for r in all_results[:len(BTC_SPOT_ETFS)] if isinstance(r, dict) and r]
    eth_results = [r for r in all_results[len(BTC_SPOT_ETFS):] if isinstance(r, dict) and r]

    def _aggregate(etfs: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not etfs:
            return {"total_aum": 0, "daily_volume": 0, "avg_change_pct": 0, "etf_count": 0, "top_etf": None}

        total_aum = sum(e.get("market_cap") or 0 for e in etfs)
        daily_volume = sum((e.get("volume") or 0) * (e.get("price") or 0) for e in etfs)
        avg_change = sum(e.get("change_pct") or 0 for e in etfs) / len(etfs)
        # En büyük ETF (AUM bazlı)
        top = max(etfs, key=lambda e: e.get("market_cap") or 0)
        return {
            "total_aum": round(total_aum, 0),
            "daily_volume": round(daily_volume, 0),
            "avg_change_pct": round(avg_change, 2),
            "etf_count": len(etfs),
            "top_etf": {"symbol": top["symbol"], "aum": top.get("market_cap")},
        }

    result = {
        "btc": _aggregate(btc_results),
        "eth": _aggregate(eth_results),
    }
    logger.info(f"ETF flows: btc_etfs={len(btc_results)}, eth_etfs={len(eth_results)}")
    return result


# ════════════════════════════════════════════════════════════════════════════
# 3. ECONOMIC CALENDAR
# ════════════════════════════════════════════════════════════════════════════

async def get_economic_calendar(limit: int = 8) -> List[Dict[str, Any]]:
    """
    Bugün açıklanacak ekonomik veriler — sadece high-impact (US/EU).
    """
    api_key = _api_key()
    if not api_key:
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"{FMP_LEGACY_URL}/economic_calendar?from={today}&to={today}&apikey={api_key}"

    async with aiohttp.ClientSession() as session:
        data = await _fmp_get(session, url)

    if not data or not isinstance(data, list):
        return []

    # Filtre: yüksek impact + ABD/EU
    high_impact_countries = {"US", "EU", "DE", "GB", "JP", "TR", "CN"}
    filtered = [
        e for e in data
        if e.get("impact") in ("High", "Medium")
        and e.get("country") in high_impact_countries
    ]

    # Time bazlı sırala
    def _time_key(e: Dict[str, Any]) -> str:
        return e.get("date") or "9999-99-99"

    filtered.sort(key=_time_key)

    formatted = []
    for e in filtered[:limit]:
        formatted.append({
            "country": e.get("country"),
            "event": e.get("event"),
            "date": e.get("date"),  # ISO
            "impact": e.get("impact"),
            "actual": e.get("actual"),
            "previous": e.get("previous"),
            "estimate": e.get("estimate"),
            "currency": e.get("currency"),
        })

    logger.info(f"Economic calendar: {len(formatted)} events today")
    return formatted


# ════════════════════════════════════════════════════════════════════════════
# 4. PRE-MARKET MOVERS
# ════════════════════════════════════════════════════════════════════════════

async def get_premarket_movers(limit: int = 5) -> Dict[str, List[Dict[str, Any]]]:
    """
    Top gainers + losers + most active stocks.
    FMP'nin /stock_market/gainers, /losers, /actives endpoint'lerini paralel çağırır.
    """
    api_key = _api_key()
    if not api_key:
        return {"gainers": [], "losers": [], "actives": []}

    urls = {
        "gainers": f"{FMP_LEGACY_URL}/stock_market/gainers?apikey={api_key}",
        "losers": f"{FMP_LEGACY_URL}/stock_market/losers?apikey={api_key}",
        "actives": f"{FMP_LEGACY_URL}/stock_market/actives?apikey={api_key}",
    }

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            _fmp_get(session, urls["gainers"]),
            _fmp_get(session, urls["losers"]),
            _fmp_get(session, urls["actives"]),
            return_exceptions=True,
        )

    def _format(items: Any) -> List[Dict[str, Any]]:
        if not isinstance(items, list):
            return []
        out = []
        for item in items[:limit]:
            out.append({
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "price": item.get("price"),
                "change": item.get("change"),
                "change_pct": item.get("changesPercentage"),
            })
        return out

    return {
        "gainers": _format(results[0]),
        "losers": _format(results[1]),
        "actives": _format(results[2]),
    }


# ════════════════════════════════════════════════════════════════════════════
# 5. EARNINGS TODAY
# ════════════════════════════════════════════════════════════════════════════

async def get_earnings_today(limit: int = 8) -> List[Dict[str, Any]]:
    """
    Bugün bilanço açıklayacak şirketler.
    Pre-market (BMO) + after-market (AMC) ayrımı yapılır.
    """
    api_key = _api_key()
    if not api_key:
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"{FMP_LEGACY_URL}/earning_calendar?from={today}&to={today}&apikey={api_key}"

    async with aiohttp.ClientSession() as session:
        data = await _fmp_get(session, url)

    if not data or not isinstance(data, list):
        return []

    # Sadece major şirketler (EPS estimate olanlar) + market cap proxy yok ama
    # epsEstimated varsa anlamlı bir takip değeri demek.
    relevant = [e for e in data if e.get("epsEstimated") is not None]

    formatted = []
    for e in relevant[:limit]:
        formatted.append({
            "symbol": e.get("symbol"),
            "date": e.get("date"),
            "time": e.get("time"),  # bmo / amc / dmh
            "eps_estimate": e.get("epsEstimated"),
            "eps_actual": e.get("eps"),
            "revenue_estimate": e.get("revenueEstimated"),
        })

    logger.info(f"Earnings today: {len(formatted)} companies")
    return formatted


# ════════════════════════════════════════════════════════════════════════════
# 6. SECTOR PERFORMANCE
# ════════════════════════════════════════════════════════════════════════════

async def get_sector_performance() -> List[Dict[str, Any]]:
    """
    11 ana sektörün günlük performansı — heatmap için.
    """
    api_key = _api_key()
    if not api_key:
        return []

    url = f"{FMP_LEGACY_URL}/sectors-performance?apikey={api_key}"

    async with aiohttp.ClientSession() as session:
        data = await _fmp_get(session, url)

    if not data or not isinstance(data, list):
        return []

    formatted = []
    for s in data:
        try:
            change_str = s.get("changesPercentage", "0%")
            # FMP "1.23%" string olarak döner, parse et
            if isinstance(change_str, str):
                change_pct = float(change_str.replace("%", "").replace("+", "").strip())
            else:
                change_pct = float(change_str or 0)
        except (ValueError, TypeError):
            change_pct = 0.0

        formatted.append({
            "sector": s.get("sector"),
            "change_pct": round(change_pct, 2),
            "trend": "up" if change_pct >= 0 else "down",
        })

    # Performansa göre sırala (en pozitiften en negatife)
    formatted.sort(key=lambda x: x["change_pct"], reverse=True)
    logger.info(f"Sector performance: {len(formatted)} sectors")
    return formatted


# ════════════════════════════════════════════════════════════════════════════
# UNIFIED FETCHER
# ════════════════════════════════════════════════════════════════════════════

async def get_full_dashboard_summary() -> Dict[str, Any]:
    """
    6 paneli paralel olarak çek. Bir panel timeout'a düşse bile diğerleri döner.
    Cache layer endpoint tarafında (HTTP Cache-Control 5 dk).
    """
    try:
        results = await asyncio.gather(
            get_overnight_markets(),
            get_etf_flows(),
            get_economic_calendar(),
            get_premarket_movers(),
            get_earnings_today(),
            get_sector_performance(),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"Dashboard summary fatal error: {e}")
        results = [None] * 6

    def _safe(result: Any, default: Any) -> Any:
        if isinstance(result, Exception) or result is None:
            return default
        return result

    return {
        "overnight_markets": _safe(results[0], {"asia": [], "europe": [], "us_futures": []}),
        "etf_flows":         _safe(results[1], {"btc": {}, "eth": {}}),
        "economic_calendar": _safe(results[2], []),
        "premarket_movers":  _safe(results[3], {"gainers": [], "losers": [], "actives": []}),
        "earnings_today":    _safe(results[4], []),
        "sector_performance":_safe(results[5], []),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
