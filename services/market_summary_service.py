"""
Market Summary Service — Dashboard "Sen Uyurken Piyasada" 6 panel için FMP entegrasyonu.

PANELLER (FMP /stable endpoint'leri):
  1. Overnight Markets   → /quote?symbol=X (her endeks için paralel)
  2. ETF Flows           → /quote?symbol=X (BTC/ETH spot ETF'leri paralel)
  3. Economic Calendar   → /economic-calendar (high-impact filtre)
  4. Pre-Market Movers   → /biggest-gainers + /biggest-losers + /most-actives
  5. Earnings Today      → /earnings-calendar
  6. Sector Performance  → /sector-performance-snapshot?date=YYYY-MM-DD

NOT: FMP'nin /api/v3 endpoint'leri 2025-08-31'den sonra legacy oldu.
Yeni aboneler /stable kullanmak zorunda. Bazı stable yanıtları farklı
field isimleriyle gelir (örn. quote → changePercentage; gainers →
changesPercentage). Burada her birini doğru parse ediyoruz.
"""
import os
import asyncio
import aiohttp
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger("market_summary_service")

FMP_BASE_URL = "https://financialmodelingprep.com/stable"

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
        ("^GSPC", "S&P 500", "🇺🇸"),
        ("^IXIC", "NASDAQ", "🇺🇸"),
        ("^DJI", "DOW JONES", "🇺🇸"),
    ],
}

# Bilinen Bitcoin/Ethereum Spot ETF'leri (US-listed)
BTC_SPOT_ETFS = ["IBIT", "FBTC", "BITB", "ARKB", "BTCO", "EZBC", "BRRR", "HODL"]
ETH_SPOT_ETFS = ["ETHA", "FETH", "ETHV", "ETHW", "QETH", "EZET"]


def _api_key() -> str:
    return os.getenv("FMP_API_KEY", "").strip()


async def _fmp_get(session: aiohttp.ClientSession, url: str, timeout: int = 8) -> Any:
    """Generic FMP GET — hata yutar, None döner."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                logger.warning(f"FMP GET {resp.status}: {url[:100]}")
                return None
            return await resp.json()
    except asyncio.TimeoutError:
        logger.warning(f"FMP GET timeout: {url[:100]}")
        return None
    except Exception as e:
        logger.warning(f"FMP GET error: {e}")
        return None


async def _fetch_quote(session: aiohttp.ClientSession, symbol: str, api_key: str) -> Optional[Dict[str, Any]]:
    """Tek bir sembol için /stable/quote çağrısı."""
    url = f"{FMP_BASE_URL}/quote?symbol={symbol}&apikey={api_key}"
    data = await _fmp_get(session, url, timeout=6)
    if not data or not isinstance(data, list) or not data:
        return None
    return data[0]


# ════════════════════════════════════════════════════════════════════════════
# 1. OVERNIGHT MARKETS
# ════════════════════════════════════════════════════════════════════════════

async def _fmp_batch_quote(
    session: aiohttp.ClientSession,
    symbols: List[str],
    api_key: str,
) -> Dict[str, Dict[str, Any]]:
    """`/stable/batch-quote` — tek HTTP çağrısıyla N sembol.

    Per-symbol `/stable/quote` `^GDAXI` / `^FCHI` / `^VIX` gibi endeksleri
    "Special Endpoint" hatası ile reddediyor (FMP Premium per-symbol kısıtlaması).
    Batch-quote bu kısıtı bypass ediyor; tüm endeksleri ve futures'ı tek istekte
    alıp dict olarak `{symbol: quote_dict}` döner.
    """
    if not symbols:
        return {}
    csv = ",".join(symbols)
    url = f"{FMP_BASE_URL}/batch-quote?symbols={csv}&apikey={api_key}"
    data = await _fmp_get(session, url, timeout=8)
    if not data or not isinstance(data, list):
        logger.warning(f"batch-quote empty/invalid for {len(symbols)} symbols")
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for q in data:
        if not isinstance(q, dict):
            continue
        sym = q.get("symbol")
        if sym:
            out[sym] = q
    return out


async def get_overnight_markets() -> Dict[str, Any]:
    """
    Asya, Avrupa endeksleri ve US futures — kullanıcı uyurken neler oldu?

    Day 28 part 5: per-symbol `/stable/quote` yerine `/stable/batch-quote` —
    `^GDAXI` (DAX) ve `^FCHI` (CAC 40) FMP'nin Special Endpoint kısıtına
    takılıyordu, batch endpoint'i bunu bypass ediyor.
    """
    api_key = _api_key()
    if not api_key:
        logger.error("FMP_API_KEY missing — overnight markets skipped")
        return {"asia": [], "europe": [], "us_futures": [], "error": "no_api_key"}

    # Tüm sembolleri toparla (region eşlemesini koru)
    sym_to_meta: Dict[str, tuple] = {}  # symbol -> (label, flag, region)
    all_symbols: List[str] = []
    for region, syms in OVERNIGHT_INDICES.items():
        for symbol, label, flag in syms:
            sym_to_meta[symbol] = (label, flag, region)
            all_symbols.append(symbol)

    async with aiohttp.ClientSession() as session:
        quotes = await _fmp_batch_quote(session, all_symbols, api_key)

    result: Dict[str, List[Dict[str, Any]]] = {"asia": [], "europe": [], "us_futures": []}

    for symbol in all_symbols:
        q = quotes.get(symbol)
        if not q:
            continue
        label, flag, region = sym_to_meta[symbol]
        # /stable/batch-quote field'ları: price, change, changePercentage
        change_pct = q.get("changePercentage") or q.get("changesPercentage") or 0
        price = q.get("price") or 0
        change = q.get("change") or 0
        result[region].append({
            "symbol": symbol,
            "label": label,
            "flag": flag,
            "price": round(price, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "trend": "up" if change_pct >= 0 else "down",
        })

    logger.info(
        f"Overnight markets: asia={len(result['asia'])}, "
        f"eu={len(result['europe'])}, us={len(result['us_futures'])} "
        f"(batch-quote, {len(quotes)}/{len(all_symbols)} resolved)"
    )
    return result


async def get_vix_quote_fmp() -> Optional[Dict[str, Any]]:
    """VIX (^VIX) — FMP batch-quote ile.

    Railway'de yfinance bloklu (Day 4 BLS pattern). FMP Premium aboneliğimiz
    `/stable/batch-quote?symbols=^VIX` ile sorunsuz dönüyor.
    """
    api_key = _api_key()
    if not api_key:
        return None
    async with aiohttp.ClientSession() as session:
        quotes = await _fmp_batch_quote(session, ["^VIX"], api_key)
    q = quotes.get("^VIX")
    if not q:
        return None
    current = q.get("price") or 0
    change_pct = q.get("changePercentage") or q.get("changesPercentage") or 0
    if not current:
        return None
    if current > 30:
        status, color = "Yüksek (Panik)", "red"
    elif current > 20:
        status, color = "Orta", "yellow"
    elif current > 15:
        status, color = "Normal", "green"
    else:
        status, color = "Düşük (Sakin)", "green"
    return {
        "current": round(float(current), 2),
        "status": status,
        "color": color,
        "change_pct": round(float(change_pct), 2),
    }


# ════════════════════════════════════════════════════════════════════════════
# 2. ETF FLOWS (BTC/ETH SPOT)
# ════════════════════════════════════════════════════════════════════════════

async def get_etf_flows() -> Dict[str, Any]:
    """
    BTC ve ETH Spot ETF'leri için aggregated metrics + coin equivalent.
    SoSoValue tarzı görüntü: USD net flow + BTC/ETH miktar.

    Hibrit yaklaşım:
      - net_flow_usd / net_flow_coins → cache (gerçek scrape data)
      - etf_count / daily_volume / top_etf / avg_change_pct → FMP (her zaman taze)
      - Cache yoksa → FMP approximation tüm field'ler (fallback)
    """
    # Önce FMP'den her iki sembol için "ETF list metrics"i çek (count, volume, top, AUM)
    fmp_full = await _get_etf_flows_fmp()

    btc_data, eth_data = await asyncio.gather(
        _get_etf_flow_with_cache("BTC", fmp_fallback=fmp_full.get("btc", {})),
        _get_etf_flow_with_cache("ETH", fmp_fallback=fmp_full.get("eth", {})),
        return_exceptions=True,
    )

    btc_result = btc_data if isinstance(btc_data, dict) else fmp_full.get("btc", {})
    eth_result = eth_data if isinstance(eth_data, dict) else fmp_full.get("eth", {})

    return {"btc": btc_result, "eth": eth_result}


async def _get_etf_flow_with_cache(
    symbol: str,
    fmp_fallback: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Cache'den net flow + FMP'den list metrics → hibrit response.
    Cache yoksa FMP fallback (mevcut approximation).
    """
    from services.etf_flow_cache_service import get_latest_etf_flow

    cached = await get_latest_etf_flow(symbol)
    if cached:
        return _format_hybrid(cached, fmp_fallback)

    # Cache miss → FMP approximation
    return fmp_fallback


def _format_hybrid(cached: Dict[str, Any], fmp: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cache + FMP enrichment.
    - net_flow_usd / net_flow_coins → cache (real scrape)
    - etf_count / top_etf / daily_volume / avg_change_pct → FMP
    - total_aum → cache varsa cache, yoksa FMP
    """
    return {
        "total_aum": cached.get("total_aum_usd") or fmp.get("total_aum") or 0,
        "daily_volume": fmp.get("daily_volume") or 0,
        "avg_change_pct": fmp.get("avg_change_pct") or 0,
        "etf_count": fmp.get("etf_count") or 0,
        "top_etf": fmp.get("top_etf"),
        "net_flow_usd": cached["net_flow_usd"],
        "net_flow_coins": cached["net_flow_coins"],
        "coin_price": cached.get("spot_price") or fmp.get("coin_price") or 0,
        "source": cached["source"],
        "scraped_at": cached["scraped_at"],
        "data_date": cached.get("data_date"),
        "age_hours": cached["age_hours"],
        "is_fresh": cached["is_fresh"],
        "is_stale": cached["is_stale"],
    }


async def _get_etf_flows_fmp() -> Dict[str, Any]:
    """
    FMP approximation — volume × price × change% (fallback veya direct).
    """
    api_key = _api_key()
    if not api_key:
        return {"btc": {}, "eth": {}, "error": "no_api_key"}

    async with aiohttp.ClientSession() as session:
        btc_tasks = [_fetch_quote(session, s, api_key) for s in BTC_SPOT_ETFS]
        eth_tasks = [_fetch_quote(session, s, api_key) for s in ETH_SPOT_ETFS]
        # Spot fiyatları paralel — coin equivalent için gerekli
        btc_price_task = _fetch_quote(session, "BTCUSD", api_key)
        eth_price_task = _fetch_quote(session, "ETHUSD", api_key)

        all_results = await asyncio.gather(
            *btc_tasks, *eth_tasks, btc_price_task, eth_price_task,
            return_exceptions=True,
        )

    btc_quotes = []
    for sym, q in zip(BTC_SPOT_ETFS, all_results[:len(BTC_SPOT_ETFS)]):
        if isinstance(q, dict) and q:
            btc_quotes.append({"symbol": sym, **q})

    eth_quotes = []
    eth_offset = len(BTC_SPOT_ETFS)
    for sym, q in zip(ETH_SPOT_ETFS, all_results[eth_offset:eth_offset + len(ETH_SPOT_ETFS)]):
        if isinstance(q, dict) and q:
            eth_quotes.append({"symbol": sym, **q})

    btc_price_q = all_results[-2]
    eth_price_q = all_results[-1]
    btc_price = float((btc_price_q or {}).get("price") or 0) if isinstance(btc_price_q, dict) else 0
    eth_price = float((eth_price_q or {}).get("price") or 0) if isinstance(eth_price_q, dict) else 0

    def _aggregate(etfs: List[Dict[str, Any]], coin_price: float) -> Dict[str, Any]:
        if not etfs:
            return {
                "total_aum": 0, "daily_volume": 0, "avg_change_pct": 0,
                "etf_count": 0, "top_etf": None,
                "net_flow_usd": 0, "net_flow_coins": 0,
                "coin_price": coin_price,
            }

        total_aum = sum(e.get("marketCap") or 0 for e in etfs)
        daily_volume = sum((e.get("volume") or 0) * (e.get("price") or 0) for e in etfs)

        def _change(e: Dict[str, Any]) -> float:
            return float(e.get("changePercentage") or e.get("changesPercentage") or 0)

        avg_change = sum(_change(e) for e in etfs) / len(etfs)

        # USD net flow: approximation — trade-value × günlük değişim
        net_flow_usd = sum(
            (e.get("volume") or 0) * (e.get("price") or 0) * (_change(e) / 100.0)
            for e in etfs
        )

        # Coin equivalent: USD / spot price → kaç BTC/ETH ekvivalan
        net_flow_coins = (net_flow_usd / coin_price) if coin_price > 0 else 0

        top = max(etfs, key=lambda e: e.get("marketCap") or 0)
        return {
            "total_aum": round(total_aum, 0),
            "daily_volume": round(daily_volume, 0),
            "avg_change_pct": round(avg_change, 2),
            "etf_count": len(etfs),
            "top_etf": {"symbol": top["symbol"], "aum": top.get("marketCap")},
            "net_flow_usd": round(net_flow_usd, 0),
            "net_flow_coins": round(net_flow_coins, 2),
            "coin_price": round(coin_price, 2),
        }

    result = {
        "btc": _aggregate(btc_quotes, btc_price),
        "eth": _aggregate(eth_quotes, eth_price),
    }
    logger.info(
        f"ETF flows [FMP]: btc={len(btc_quotes)}@${btc_price:.0f}, "
        f"eth={len(eth_quotes)}@${eth_price:.0f}"
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
# 3. ECONOMIC CALENDAR
# ════════════════════════════════════════════════════════════════════════════

async def get_economic_calendar(limit: int = 8) -> List[Dict[str, Any]]:
    """Bugün açıklanacak ekonomik veriler — high/medium impact + major countries."""
    api_key = _api_key()
    if not api_key:
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"{FMP_BASE_URL}/economic-calendar?from={today}&to={today}&apikey={api_key}"

    async with aiohttp.ClientSession() as session:
        data = await _fmp_get(session, url)

    if not data or not isinstance(data, list):
        return []

    # Kullanıcı talebiyle (2026-05-13) sadece 5 majör bölge:
    # ABD, AB, Türkiye, Çin, Japonya. DE/GB/FR ayrıca AB ile redundant,
    # TR portföy için doğrudan ilgili, US/CN/JP makro risk yönü.
    high_impact_countries = {"US", "EU", "TR", "CN", "JP"}
    filtered = [
        e for e in data
        if e.get("impact") in ("High", "Medium")
        and e.get("country") in high_impact_countries
    ]

    # ISO datetime ile sırala
    filtered.sort(key=lambda e: e.get("date") or "9999-99-99")

    formatted = []
    for e in filtered[:limit]:
        formatted.append({
            "country": e.get("country"),
            "event": e.get("event"),
            "date": e.get("date"),
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
    """Top gainers + losers + most active (FMP /stable endpoint'leri)."""
    api_key = _api_key()
    if not api_key:
        return {"gainers": [], "losers": [], "actives": []}

    urls = {
        "gainers": f"{FMP_BASE_URL}/biggest-gainers?apikey={api_key}",
        "losers": f"{FMP_BASE_URL}/biggest-losers?apikey={api_key}",
        "actives": f"{FMP_BASE_URL}/most-actives?apikey={api_key}",
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
                # gainers/losers/actives: changesPercentage (s'li)
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
    """Bugün bilanço açıklayacak şirketler (epsEstimated olanlar prioritized)."""
    api_key = _api_key()
    if not api_key:
        return []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"{FMP_BASE_URL}/earnings-calendar?from={today}&to={today}&apikey={api_key}"

    async with aiohttp.ClientSession() as session:
        data = await _fmp_get(session, url)

    if not data or not isinstance(data, list):
        return []

    # Sadece anlamlı estimate'i olanlar (büyük şirketler genelde estimate olur)
    relevant = [e for e in data if e.get("epsEstimated") is not None]

    # Estimate yoksa hâlâ bir şey göstermek için backup'a düşelim
    if not relevant:
        relevant = data[:limit]

    formatted = []
    for e in relevant[:limit]:
        formatted.append({
            "symbol": e.get("symbol"),
            "date": e.get("date"),
            "time": e.get("time"),  # bmo / amc / dmh — stable'da yok olabilir
            "eps_estimate": e.get("epsEstimated"),
            "eps_actual": e.get("epsActual") or e.get("eps"),
            "revenue_estimate": e.get("revenueEstimated"),
        })

    logger.info(f"Earnings today: {len(formatted)} companies")
    return formatted


# ════════════════════════════════════════════════════════════════════════════
# 6. SECTOR PERFORMANCE
# ════════════════════════════════════════════════════════════════════════════

async def get_sector_performance() -> List[Dict[str, Any]]:
    """11 ana sektörün günlük performansı — heatmap için."""
    api_key = _api_key()
    if not api_key:
        return []

    # /stable/sector-performance-snapshot date parametresi gerektiriyor
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"{FMP_BASE_URL}/sector-performance-snapshot?date={today}&apikey={api_key}"

    async with aiohttp.ClientSession() as session:
        data = await _fmp_get(session, url)

    # Bugün veri yoksa dün dene (hafta sonu/tatil günleri için)
    if not data or not isinstance(data, list) or len(data) == 0:
        from datetime import timedelta
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        url = f"{FMP_BASE_URL}/sector-performance-snapshot?date={yesterday}&apikey={api_key}"
        async with aiohttp.ClientSession() as session:
            data = await _fmp_get(session, url)

    if not data or not isinstance(data, list):
        return []

    # Aynı sektör birden çok exchange için gelebilir — sektöre göre averaj
    sector_map: Dict[str, List[float]] = {}
    for s in data:
        sector_name = s.get("sector")
        avg_change = s.get("averageChange")
        if sector_name is None or avg_change is None:
            continue
        try:
            sector_map.setdefault(sector_name, []).append(float(avg_change))
        except (ValueError, TypeError):
            continue

    formatted = []
    for sector, values in sector_map.items():
        avg = sum(values) / len(values) if values else 0.0
        formatted.append({
            "sector": sector,
            "change_pct": round(avg, 2),
            "trend": "up" if avg >= 0 else "down",
        })

    formatted.sort(key=lambda x: x["change_pct"], reverse=True)
    logger.info(f"Sector performance: {len(formatted)} sectors")
    return formatted


# ════════════════════════════════════════════════════════════════════════════
# 7. FEAR INDICES (VIX + Crypto Fear & Greed)
# ════════════════════════════════════════════════════════════════════════════

def _vix_status(level: float) -> tuple:
    """VIX seviyesine göre Türkçe etiket + renk."""
    if level > 30:
        return ("Yüksek (Panik)", "red")
    if level > 20:
        return ("Orta", "yellow")
    if level > 15:
        return ("Normal", "green")
    return ("Düşük (Sakin)", "green")


def _fng_status(value: int) -> tuple:
    """Crypto Fear & Greed (0-100) seviyesine göre etiket + renk."""
    if value <= 24:
        return ("Aşırı Korku", "red")
    if value <= 44:
        return ("Korku", "yellow")
    if value <= 55:
        return ("Nötr", "yellow")
    if value <= 74:
        return ("Açgözlülük", "green")
    return ("Aşırı Açgözlülük", "red")  # paradoxically dangerous


async def _get_vix_async() -> Optional[Dict[str, Any]]:
    """
    VIX (S&P 500 volatility) — FMP /stable/quote ile.
    yfinance Railway IP'lerinde rate-limit yiyor; FMP bu sembolü destekliyor.
    """
    api_key = _api_key()
    if not api_key:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            q = await _fetch_quote(session, "^VIX", api_key)
        if not q:
            return None
        current = float(q.get("price") or 0)
        prev = float(q.get("previousClose") or current)
        change_pct = q.get("changePercentage")
        if change_pct is None:
            change_pct = ((current - prev) / prev * 100) if prev > 0 else 0
        label, color = _vix_status(current)
        return {
            "current": round(current, 2),
            "label": label,
            "color": color,
            "change_pct": round(float(change_pct), 2),
            "prev_close": round(prev, 2),
        }
    except Exception as e:
        logger.warning(f"VIX (FMP) fetch error: {e}")
        return None


async def _get_crypto_fng() -> Optional[Dict[str, Any]]:
    """Crypto Fear & Greed Index — alternative.me free API."""
    url = "https://api.alternative.me/fng/?limit=2"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                if resp.status != 200:
                    logger.warning(f"F&G API HTTP {resp.status}")
                    return None
                data = await resp.json()
        items = data.get("data") or []
        if not items:
            return None
        today = items[0]
        yesterday = items[1] if len(items) > 1 else None

        try:
            value = int(today.get("value") or 0)
        except (ValueError, TypeError):
            return None

        prev_value = None
        if yesterday:
            try:
                prev_value = int(yesterday.get("value") or 0)
            except (ValueError, TypeError):
                prev_value = None

        label, color = _fng_status(value)
        change = (value - prev_value) if prev_value is not None else None

        return {
            "value": value,                    # 0-100
            "label": label,                    # TR
            "label_en": today.get("value_classification"),  # EN
            "color": color,
            "prev_value": prev_value,
            "change": change,
        }
    except Exception as e:
        logger.warning(f"F&G fetch error: {e}")
        return None


async def get_fear_indices() -> Dict[str, Any]:
    """VIX (FMP) + Crypto Fear & Greed (alternative.me) — paralel."""
    vix, fng = await asyncio.gather(
        _get_vix_async(),
        _get_crypto_fng(),
        return_exceptions=True,
    )
    return {
        "vix": vix if isinstance(vix, dict) else None,
        "crypto_fng": fng if isinstance(fng, dict) else None,
    }


# ════════════════════════════════════════════════════════════════════════════
# UNIFIED FETCHER
# ════════════════════════════════════════════════════════════════════════════

async def get_full_dashboard_summary() -> Dict[str, Any]:
    """6 paneli paralel çek; bir panel başarısız olsa diğerleri döner.

    NOT (2026-05-12): get_overnight_markets() devre dışı — FMP Starter plan
    Asya/Avrupa endekslerini 402 reddediyor, her poll cycle'da boş döndürüp
    bandwidth/log gürültüsü yaratıyordu. Veri üst ticker tape'e taşındı
    (dashboard /api/ticker, Yahoo Finance fallback ile). FMP Ultimate'a
    çıkılırsa get_overnight_markets() çağrısı geri açılabilir.
    """
    try:
        results = await asyncio.gather(
            get_etf_flows(),
            get_economic_calendar(),
            get_premarket_movers(),
            get_earnings_today(),
            get_sector_performance(),
            get_fear_indices(),
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
        "overnight_markets": {"asia": [], "europe": [], "us_futures": []},
        "etf_flows":         _safe(results[0], {"btc": {}, "eth": {}}),
        "economic_calendar": _safe(results[1], []),
        "premarket_movers":  _safe(results[2], {"gainers": [], "losers": [], "actives": []}),
        "earnings_today":    _safe(results[3], []),
        "sector_performance":_safe(results[4], []),
        "fear_indices":      _safe(results[5], {"vix": None, "crypto_fng": None}),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
