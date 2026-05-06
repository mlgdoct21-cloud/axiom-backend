"""
Market-Wide CryptoQuant Intelligence — beyond BTC/ETH/XRP.

Provides three composite views:
  1. ERC20 Radar    — DeFi token akıllı para hareketi (9 token)
  2. Stablecoin Pulse — USDC + DAI flows + SSR proxy
  3. Alt Season Score — composite altcoin climate gauge (0-100)

Each public function returns a dict ready for JSON serialization.
All results cached in cryptoquant_cache (4h-12h TTL depending on volatility).
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from core.logger import get_logger
from services.cryptoquant_service import (
    _cq_get,
    _cache_get,
    _cache_set,
    _is_configured,
    _yesterday_str,
)

logger = get_logger("cryptoquant_market")


# ── ERC20 Radar ────────────────────────────────────────────────────────────

# CryptoQuant Pro plan'da netflow/reserve/supply-ratio sunduğu DeFi tokenlar.
# Canlı testle doğrulananlar (2026-05-06).
_ERC20_TOKENS: list[dict] = [
    {"symbol": "LINK",  "token": "link",  "name": "Chainlink"},
    {"symbol": "UNI",   "token": "uni",   "name": "Uniswap"},
    {"symbol": "AAVE",  "token": "aave",  "name": "Aave"},
    {"symbol": "CRV",   "token": "crv",   "name": "Curve"},
    {"symbol": "MKR",   "token": "mkr",   "name": "Maker"},
    {"symbol": "SNX",   "token": "snx",   "name": "Synthetix"},
    {"symbol": "COMP",  "token": "comp",  "name": "Compound"},
    {"symbol": "MATIC", "token": "matic", "name": "Polygon"},
    {"symbol": "SHIB",  "token": "shib",  "name": "Shiba Inu"},
]


async def _fetch_erc20_token(token: str) -> Optional[dict]:
    """Tek bir ERC20 için 7 günlük netflow + son reserve + supply-ratio."""
    yesterday = _yesterday_str()
    date_7d = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y%m%d")

    # 7-day netflow → cumulative direction
    netflow_raw = await _cq_get(
        "/erc20/exchange-flows/netflow",
        {"token": token, "exchange": "all_exchange", "window": "day",
         "from": date_7d, "limit": 8},
    )
    if not netflow_raw:
        return None
    nf_rows = netflow_raw.get("result", {}).get("data", [])
    if not nf_rows:
        return None

    total_7d = sum(float(r.get("netflow_total", 0)) for r in nf_rows)
    latest_1d = float(nf_rows[-1].get("netflow_total", 0))

    # Reserve (latest)
    reserve_raw = await _cq_get(
        "/erc20/exchange-flows/reserve",
        {"token": token, "exchange": "all_exchange", "window": "day",
         "from": yesterday, "limit": 1},
    )
    reserve = None
    reserve_usd = None
    if reserve_raw:
        rsv_rows = reserve_raw.get("result", {}).get("data", [])
        if rsv_rows:
            reserve = float(rsv_rows[-1].get("reserve", 0))
            reserve_usd = float(rsv_rows[-1].get("reserve_usd", 0))

    # Netflow-to-reserve ratio (auto-scales across token sizes).
    # Reserve fetch rate-limit'e takılmış olabilir → o zaman 7G netflow
    # yönüne göre coarse sinyal üret.
    ratio = (latest_1d / reserve * 100) if reserve else None

    if ratio is not None:
        # Eşikler hafifletildi (LINK: -0.04%, UNI: -0.25% gibi tipik
        # değerleri yakalamak için 0.2 → 0.05).
        if ratio < -0.5:
            signal, label = "STRONG_BULLISH", "💎 Güçlü Birikim"
        elif ratio < -0.05:
            signal, label = "BULLISH", "🟢 Birikim"
        elif ratio > 0.5:
            signal, label = "STRONG_BEARISH", "🔴 Güçlü Dağıtım"
        elif ratio > 0.05:
            signal, label = "BEARISH", "⚠️ Dağıtım"
        else:
            signal, label = "NEUTRAL", "🟡 Nötr"
    else:
        # Fallback: reserve eksik → sadece netflow yönüne bak
        if total_7d < 0:
            signal, label = "BULLISH", "🟢 7G Net Çıkış"
        elif total_7d > 0:
            signal, label = "BEARISH", "⚠️ 7G Net Giriş"
        else:
            signal, label = "NEUTRAL", "🟡 Nötr"

    return {
        "token": token,
        "netflow_1d": latest_1d,
        "netflow_7d": total_7d,
        "reserve": reserve,
        "reserve_usd": reserve_usd,
        "netflow_to_reserve_pct": round(ratio, 3),
        "signal": signal,
        "label_tr": label,
        "date": nf_rows[-1].get("date"),
    }


async def get_erc20_radar() -> dict:
    """9 ERC20 token için akıllı para haritası."""
    if not _is_configured():
        return {"error": "cryptoquant_not_configured"}

    cached = await _cache_get("erc20_radar", "all", "day")
    if cached:
        return cached

    # Sequential to avoid rate-limit (1.0s between calls is safe)
    results = []
    for cfg in _ERC20_TOKENS:
        data = await _fetch_erc20_token(cfg["token"])
        if data:
            results.append({
                "symbol": cfg["symbol"],
                "name": cfg["name"],
                **data,
            })
        await asyncio.sleep(2.0)

    # Aggregate sinyal: STRONG_BULLISH=+2, BULLISH=+1, NEUTRAL=0, BEARISH=-1, STRONG_BEARISH=-2
    score_map = {"STRONG_BULLISH": 2, "BULLISH": 1, "NEUTRAL": 0, "BEARISH": -1, "STRONG_BEARISH": -2}
    score_total = sum(score_map.get(r["signal"], 0) for r in results)
    max_score = len(results) * 2
    aggregate_pct = round((score_total / max_score) * 100, 1) if max_score else 0

    if aggregate_pct >= 50:
        agg_label = "💎 ERC20 Güçlü Birikim"
    elif aggregate_pct >= 20:
        agg_label = "🟢 ERC20 Hafif Birikim"
    elif aggregate_pct <= -50:
        agg_label = "🔴 ERC20 Güçlü Dağıtım"
    elif aggregate_pct <= -20:
        agg_label = "⚠️ ERC20 Hafif Dağıtım"
    else:
        agg_label = "🟡 ERC20 Karışık"

    payload = {
        "tokens": results,
        "aggregate_score_pct": aggregate_pct if results else None,
        "aggregate_label_tr": agg_label,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # En az 3 token başarılıysa cache'le, yoksa partial veriyi tekrar çekmeye
    # bırak (gelecek çağrı taze fetch'le tamamlamaya çalışsın).
    if len(results) >= 3:
        await _cache_set("erc20_radar", "all", "day", payload, timedelta(hours=4))
    return payload


# ── Stablecoin Pulse ──────────────────────────────────────────────────────

_STABLECOINS: list[dict] = [
    {"symbol": "USDC", "token": "usdc", "name": "USD Coin"},
    {"symbol": "DAI",  "token": "dai",  "name": "DAI"},
]


async def _fetch_stablecoin_token(token: str) -> Optional[dict]:
    """Tek bir stablecoin için reserve + 7G netflow + günlük inflow/outflow."""
    yesterday = _yesterday_str()
    date_7d = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y%m%d")

    netflow_raw = await _cq_get(
        "/stablecoin/exchange-flows/netflow",
        {"token": token, "exchange": "all_exchange", "window": "day",
         "from": date_7d, "limit": 8},
    )
    if not netflow_raw:
        return None
    nf_rows = netflow_raw.get("result", {}).get("data", [])
    if not nf_rows:
        return None

    netflow_7d = sum(float(r.get("netflow_total", 0)) for r in nf_rows)
    netflow_1d = float(nf_rows[-1].get("netflow_total", 0))

    reserve_raw = await _cq_get(
        "/stablecoin/exchange-flows/reserve",
        {"token": token, "exchange": "all_exchange", "window": "day",
         "from": yesterday, "limit": 1},
    )
    reserve = 0.0
    if reserve_raw:
        rsv_rows = reserve_raw.get("result", {}).get("data", [])
        if rsv_rows:
            reserve = float(rsv_rows[-1].get("reserve", 0))

    inflow_raw = await _cq_get(
        "/stablecoin/exchange-flows/inflow",
        {"token": token, "exchange": "all_exchange", "window": "day",
         "from": yesterday, "limit": 1},
    )
    inflow_1d = 0.0
    if inflow_raw:
        in_rows = inflow_raw.get("result", {}).get("data", [])
        if in_rows:
            inflow_1d = float(in_rows[-1].get("inflow_total", 0))

    return {
        "token": token,
        "reserve": reserve,
        "netflow_1d": netflow_1d,
        "netflow_7d": netflow_7d,
        "inflow_1d": inflow_1d,
        "date": nf_rows[-1].get("date"),
    }


async def _fetch_btc_market_cap_proxy() -> float:
    """BTC market cap proxy: spot price × ~19.7M circulating.
    SSR (Stablecoin Supply Ratio) hesaplaması için. Tam değil, proxy."""
    raw = await _cq_get(
        "/btc/market-data/price-ohlcv",
        {"market": "spot", "exchange": "binance", "symbol": "btc_usdt",
         "window": "hour", "limit": 1},
    )
    if not raw:
        return 0.0
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return 0.0
    price = float(rows[-1].get("close") or 0)
    BTC_CIRCULATING_SUPPLY = 19_700_000  # 2026 itibarıyla yaklaşık
    return price * BTC_CIRCULATING_SUPPLY


async def get_stablecoin_pulse() -> dict:
    """Stablecoin akış nabzı + SSR proxy.
    Yüksek inflow + düşük SSR = altcoin için yeşil ışık."""
    if not _is_configured():
        return {"error": "cryptoquant_not_configured"}

    cached = await _cache_get("stablecoin_pulse", "all", "day")
    if cached:
        return cached

    results = []
    for cfg in _STABLECOINS:
        data = await _fetch_stablecoin_token(cfg["token"])
        if data:
            results.append({"symbol": cfg["symbol"], "name": cfg["name"], **data})
        await asyncio.sleep(2.0)

    btc_mcap = await _fetch_btc_market_cap_proxy()
    total_reserve = sum(r["reserve"] for r in results)
    total_inflow_1d = sum(r["inflow_1d"] for r in results)
    total_netflow_1d = sum(r["netflow_1d"] for r in results)
    total_netflow_7d = sum(r["netflow_7d"] for r in results)

    # SSR proxy = BTC market cap / stablecoin reserve on exchanges.
    # Düşük SSR = bol kuru barut = altcoinler için yeşil ışık.
    # btc_mcap 0 ise (rate limit) None yap, yoksa label yanlış oluyor.
    ssr_proxy = (btc_mcap / total_reserve) if (total_reserve and btc_mcap > 0) else None

    # Ana sinyal: 7-günlük net giriş yönü
    if total_netflow_7d > 1_000_000_000:  # 1B$ üstü 7G giriş
        flow_signal = "STRONG_BULLISH"
        flow_label = "💎 Yoğun Kuru Barut Birikimi"
    elif total_netflow_7d > 200_000_000:
        flow_signal = "BULLISH"
        flow_label = "🟢 Borsalara Nakit Akıyor"
    elif total_netflow_7d < -1_000_000_000:
        flow_signal = "BEARISH"
        flow_label = "🔴 Borsalardan Çıkış"
    elif total_netflow_7d < -200_000_000:
        flow_signal = "BEARISH"
        flow_label = "⚠️ Hafif Çıkış"
    else:
        flow_signal = "NEUTRAL"
        flow_label = "🟡 Dengeli Akış"

    # SSR yorumu (düşük = bol stablecoin)
    if ssr_proxy is None:
        ssr_label = "❓"
    elif ssr_proxy < 50:
        ssr_label = "💎 Çok Düşük SSR — Bol Kuru Barut"
    elif ssr_proxy < 100:
        ssr_label = "🟢 Düşük SSR — Sağlıklı Likidite"
    elif ssr_proxy < 200:
        ssr_label = "🟡 Orta SSR"
    else:
        ssr_label = "🔴 Yüksek SSR — Likidite Kıt"

    payload = {
        "tokens": results,
        "totals": {
            "reserve_usd": round(total_reserve, 2),
            "netflow_1d": round(total_netflow_1d, 2),
            "netflow_7d": round(total_netflow_7d, 2),
            "inflow_1d": round(total_inflow_1d, 2),
        },
        "ssr_proxy": round(ssr_proxy, 1) if ssr_proxy else None,
        "ssr_label_tr": ssr_label,
        "btc_mcap_proxy": round(btc_mcap, 0),
        "flow_signal": flow_signal,
        "flow_label_tr": flow_label,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # En az 1 stablecoin başarılı + reserve > 0 ise cache'le
    if results and total_reserve > 0:
        await _cache_set("stablecoin_pulse", "all", "day", payload, timedelta(hours=4))
    return payload


# ── Alt Season Composite Score ─────────────────────────────────────────────

async def get_altseason_score() -> dict:
    """5 girdili composite alt sezon pusulası (0-100).
    Yüksek skor = altcoin için elverişli ortam.

    Girdiler:
      1. Stablecoin 7G netflow yönü (%25)
      2. ETH funding rates (%20) — negatif funding = ETH altın ucuz, alts yükselebilir
      3. ERC20 aggregate netflow score (%20)
      4. XRP funding rate (%20) — altcoin türev iştahı
      5. BTC vs Stablecoin reserve dengesi (%15)
    """
    if not _is_configured():
        return {"error": "cryptoquant_not_configured"}

    cached = await _cache_get("altseason_score", "all", "day")
    if cached:
        return cached

    # Toplu veri çek
    pulse = await get_stablecoin_pulse()
    radar = await get_erc20_radar()

    # ETH + XRP funding
    from services.cryptoquant_service import (
        _fetch_eth_funding_rates,
        _fetch_xrp_funding_rates,
    )
    eth_funding, xrp_funding = await asyncio.gather(
        _fetch_eth_funding_rates(),
        _fetch_xrp_funding_rates(),
    )

    components = []
    score_total = 0.0
    weight_total = 0.0

    # 1. Stablecoin flow (25 ağırlık)
    if pulse and "totals" in pulse:
        nf7 = pulse["totals"]["netflow_7d"]
        if nf7 > 1_000_000_000:
            c, label = +25, "💎 Bol kuru barut girişi"
        elif nf7 > 200_000_000:
            c, label = +12, "🟢 Hafif kuru barut girişi"
        elif nf7 < -1_000_000_000:
            c, label = -25, "🔴 Stablecoin kaçışı"
        elif nf7 < -200_000_000:
            c, label = -12, "⚠️ Hafif stablecoin çıkışı"
        else:
            c, label = 0, "🟡 Stabil stablecoin akışı"
        components.append({"name": "Stablecoin Akışı", "weight": 25,
                           "contribution": c, "label_tr": label})
        score_total += c
        weight_total += 25

    # 2. ETH funding (20 ağırlık) — negatif = ETH'e karşı bahis = alts'a fırsat
    if eth_funding:
        avg = eth_funding["avg_24h"]
        if avg < -0.005:
            c, label = +20, "🟢 ETH'e karşı kısa bahisler (alt fırsatı)"
        elif avg < 0:
            c, label = +10, "🟢 ETH funding hafif negatif"
        elif avg > 0.01:
            c, label = -20, "🔴 ETH'te aşırı pozitif funding"
        elif avg > 0.005:
            c, label = -10, "⚠️ ETH funding ısınıyor"
        else:
            c, label = 0, "🟡 ETH funding nötr"
        components.append({"name": "ETH Funding", "weight": 20,
                           "contribution": c, "label_tr": label})
        score_total += c
        weight_total += 20

    # 3. ERC20 aggregate (20 ağırlık) — radar'da hiç token yoksa skip
    if radar and radar.get("aggregate_score_pct") is not None:
        agg = float(radar["aggregate_score_pct"])
        # agg ∈ [-100, +100] → ağırlığa scale
        c = round((agg / 100) * 20, 1)
        components.append({
            "name": "ERC20 Akıllı Para",
            "weight": 20,
            "contribution": c,
            "label_tr": radar.get("aggregate_label_tr", ""),
        })
        score_total += c
        weight_total += 20

    # 4. XRP funding (20 ağırlık) — altcoin bellwether
    if xrp_funding:
        avg = xrp_funding["avg_24h"]
        if avg < -0.005:
            c, label = +20, "🟢 XRP funding negatif (altcoin tabanı)"
        elif avg < 0:
            c, label = +10, "🟢 XRP funding hafif negatif"
        elif avg > 0.01:
            c, label = -15, "🔴 XRP'te aşırı pozitif funding"
        elif avg > 0.005:
            c, label = -8, "⚠️ XRP funding ısınıyor"
        else:
            c, label = 0, "🟡 XRP funding nötr"
        components.append({"name": "XRP Funding (Bellwether)", "weight": 20,
                           "contribution": c, "label_tr": label})
        score_total += c
        weight_total += 20

    # 5. SSR durumu (15 ağırlık)
    if pulse and pulse.get("ssr_proxy") is not None:
        ssr = pulse["ssr_proxy"]
        if ssr < 50:
            c, label = +15, "💎 Çok düşük SSR (bol nakit)"
        elif ssr < 100:
            c, label = +8, "🟢 Düşük SSR"
        elif ssr > 200:
            c, label = -15, "🔴 Yüksek SSR (likidite kıt)"
        elif ssr > 150:
            c, label = -8, "⚠️ Yükselen SSR"
        else:
            c, label = 0, "🟡 SSR normal"
        components.append({"name": "SSR (BTC/Stablecoin)", "weight": 15,
                           "contribution": c, "label_tr": label})
        score_total += c
        weight_total += 15

    if weight_total == 0:
        score = None
        zone = "UNKNOWN"
        zone_tr = "❓ Veri Yok"
    else:
        # signed_total ∈ [-weight_total, +weight_total] → 0-100 scale
        score = round(50 + (score_total / weight_total) * 50, 1)
        score = max(0, min(100, score))
        if score >= 80:
            zone, zone_tr = "STRONG_ALT_SEASON", "🔥 Güçlü Alt Sezon"
        elif score >= 65:
            zone, zone_tr = "ALT_FAVORED", "🟢 Altcoin Lehine"
        elif score >= 45:
            zone, zone_tr = "MIXED", "🟡 Karışık Ortam"
        elif score >= 30:
            zone, zone_tr = "BTC_FAVORED", "🟠 BTC Lehine"
        else:
            zone, zone_tr = "BTC_DOMINANT", "🔴 BTC Hakim"

    payload = {
        "altseason_score": score,
        "zone": zone,
        "zone_tr": zone_tr,
        "components": components,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    # En az 3 component varsa cache'le (yoksa anlamlı skor değil)
    if len(components) >= 3:
        await _cache_set("altseason_score", "all", "day", payload, timedelta(hours=4))
    return payload


# ── Refresh helper (scheduler için) ────────────────────────────────────────

async def refresh_market_metrics() -> None:
    """All market-wide metrics force-refresh."""
    logger.info("CryptoQuant Market: refreshing radar + pulse + altseason...")
    try:
        from sqlalchemy import text
        from core.database import engine
        sql = text("""
            DELETE FROM cryptoquant_cache
            WHERE metric_key IN ('erc20_radar', 'stablecoin_pulse', 'altseason_score')
        """)
        async with engine.begin() as conn:
            await conn.execute(sql)
        # Sequential — altseason depends on the other two
        await get_erc20_radar()
        await get_stablecoin_pulse()
        await get_altseason_score()
        logger.info("CryptoQuant Market: refresh complete")
    except Exception as e:
        logger.error(f"Market refresh error: {e}")
