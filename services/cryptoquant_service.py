"""
CryptoQuant On-Chain Intelligence Service.

Fetches key on-chain metrics via CryptoQuant REST API (Bearer token auth)
and caches results in the `cryptoquant_cache` Postgres table.

Metrics collected (Kademe 1 — highest signal value):
  - BTC exchange netflow       (all exchanges, daily)
  - BTC exchange whale ratio   (all exchanges, daily)
  - BTC miner outflow          (all miners, daily)
  - USDT exchange inflow       (all exchanges, daily)

Metrics collected (Kademe 2 — derivatives sentiment):
  - BTC funding rate           (Binance, hourly → aggregated daily avg)
  - BTC open interest          (all exchanges, daily)

Metrics collected (Kademe 3 — cycle indicators):
  - SOPR                       (daily)
  - Coinbase premium index     (hourly → latest value)
"""
import os
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

from sqlalchemy import text
from core.database import engine
from core.logger import get_logger

logger = get_logger("cryptoquant_service")

_BASE_URL = "https://api.cryptoquant.com/v1"


def _api_key() -> str:
    """Read fresh from env on every call so Railway env updates are picked
    up without a hard restart (module-level cache would freeze the value
    at import time)."""
    return os.getenv("CRYPTOQUANT_API_KEY", "").strip()


# Cache TTLs
_TTL_EXCHANGE_FLOW = timedelta(hours=4)
_TTL_MINER = timedelta(hours=12)
_TTL_FUNDING = timedelta(minutes=30)
_TTL_CYCLE = timedelta(hours=12)


def _is_configured() -> bool:
    return bool(_api_key())


async def _cq_get(path: str, params: dict) -> Optional[dict]:
    """Single CryptoQuant API GET call. Returns parsed JSON or None on error."""
    key = _api_key()
    if not key:
        return None
    url = f"{_BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {key}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, params=params,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 429:
                    logger.warning(f"CryptoQuant rate limit: {path}")
                    return None
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"CryptoQuant {resp.status} @ {path}: {body[:200]}")
                    return None
                return await resp.json()
    except Exception as e:
        logger.error(f"CryptoQuant request error {path}: {e}")
        return None


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _yesterday_str() -> str:
    d = datetime.now(timezone.utc) - timedelta(days=1)
    return d.strftime("%Y%m%d")


# ── Cache helpers ──────────────────────────────────────────────────────────────

async def _cache_set(metric_key: str, symbol: str, window: str, data: dict, ttl: timedelta) -> None:
    expires_at = datetime.now(timezone.utc) + ttl
    # NOTE: "window" is a PostgreSQL reserved keyword (window functions),
    # so we must double-quote it everywhere we reference the column.
    # NOTE: Use CAST(:data AS jsonb) instead of :data::jsonb — asyncpg's
    # parameter parser treats a second ":" inside the literal as another
    # bind, raising "syntax error at or near :".
    sql = text("""
        INSERT INTO cryptoquant_cache (metric_key, symbol, "window", data, fetched_at, expires_at)
        VALUES (:key, :sym, :win, CAST(:data AS jsonb), NOW(), :exp)
        ON CONFLICT (metric_key, symbol, "window")
        DO UPDATE SET data = EXCLUDED.data, fetched_at = NOW(), expires_at = EXCLUDED.expires_at
    """)
    import json
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, {
                "key": metric_key, "sym": symbol, "win": window,
                "data": json.dumps(data), "exp": expires_at,
            })
    except Exception as e:
        logger.warning(f"Cache set error {metric_key}: {e}")


async def _cache_get(metric_key: str, symbol: str, window: str) -> Optional[dict]:
    sql = text("""
        SELECT data FROM cryptoquant_cache
        WHERE metric_key = :key AND symbol = :sym AND "window" = :win
          AND expires_at > NOW()
        LIMIT 1
    """)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, {"key": metric_key, "sym": symbol, "win": window})).fetchone()
            if row:
                return dict(row[0]) if isinstance(row[0], dict) else row[0]
    except Exception as e:
        logger.warning(f"Cache get error {metric_key}: {e}")
    return None


# ── Metric fetchers ────────────────────────────────────────────────────────────

async def _fetch_exchange_netflow() -> Optional[dict]:
    """BTC exchange netflow (all exchanges, last day)."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/exchange-flows/netflow",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    return {
        "netflow_total": float(latest.get("netflow_total", 0)),
        "inflow_total": float(latest.get("inflow_total", 0)),
        "outflow_total": float(latest.get("outflow_total", 0)),
        "date": latest.get("date", yesterday),
    }


async def _fetch_whale_ratio() -> Optional[dict]:
    """BTC exchange whale ratio (all exchanges, last day). >= 0.85 = whale activity."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/flow-indicator/exchange-whale-ratio",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    # CryptoQuant returns the field as 'exchange_whale_ratio'; fall back to
    # 'whale_ratio' for older payloads.
    val = latest.get("exchange_whale_ratio")
    if val is None:
        val = latest.get("whale_ratio", 0)
    return {
        "whale_ratio": float(val),
        "date": latest.get("date", yesterday),
    }


async def _fetch_miner_outflow() -> Optional[dict]:
    """BTC miner outflow (all miners, last 2 days for trend)."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/btc/miner-flows/outflow",
        {"miner": "all_miner", "window": "day", "from": date_from, "limit": 7},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    avg_7d = sum(float(r.get("outflow_total", 0)) for r in rows) / len(rows)
    return {
        "outflow_total": float(latest.get("outflow_total", 0)),
        "avg_7d": avg_7d,
        "date": latest.get("date"),
    }


async def _fetch_miner_reserve() -> Optional[dict]:
    """BTC miner reserve trend (7-day change %)."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/btc/miner-flows/reserve",
        {"miner": "all_miner", "window": "day", "from": date_from, "limit": 8},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if len(rows) < 2:
        return None
    latest = float(rows[-1].get("reserve", 0))
    oldest = float(rows[0].get("reserve", 1))
    change_7d_pct = ((latest - oldest) / oldest * 100) if oldest else 0
    return {
        "reserve": latest,
        "change_7d_pct": round(change_7d_pct, 2),
        "date": rows[-1].get("date"),
    }


async def _fetch_stablecoin_inflow() -> Optional[dict]:
    """Stablecoin (all tokens) exchange inflow. Rising = bullish (buying power
    coming on-chain). Path moved from /usdt/... to /stablecoin/... in CQ v1
    namespace; token=all_token aggregates USDT+USDC+BUSD+DAI etc."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/stablecoin/exchange-flows/inflow",
        {"token": "all_token", "exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    return {
        "inflow_total": float(latest.get("inflow_total", 0)),
        "date": latest.get("date", yesterday),
    }


async def _fetch_funding_rates() -> Optional[dict]:
    """BTC funding rates (Binance, daily). Pro plan'da window=hour 403 dönüyor."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/btc/market-data/funding-rates",
        {"exchange": "binance", "window": "day", "from": date_from, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    values = [float(r.get("funding_rates", 0)) for r in rows if r.get("funding_rates") is not None]
    if not values:
        return None
    return {
        "latest": values[-1],
        "avg_24h": values[-1],  # daily resolution → "son 24s" ≈ son günkü değer
        "ts": rows[-1].get("date"),
    }


async def _fetch_open_interest() -> Optional[dict]:
    """BTC open interest (all exchanges, last day)."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/market-data/open-interest",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else latest
    oi = float(latest.get("open_interest", 0))
    oi_prev = float(prev.get("open_interest", 1))
    change_pct = ((oi - oi_prev) / oi_prev * 100) if oi_prev else 0
    return {
        "open_interest": oi,
        "change_pct": round(change_pct, 2),
        "date": latest.get("date"),
    }


async def _fetch_sopr() -> Optional[dict]:
    """SOPR (Spent Output Profit Ratio). >1 = profit-taking, <1 = panic selling.
    NOTE: lives under /market-indicator/, not /network-indicator/ in CQ v1."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/market-indicator/sopr",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    return {
        "sopr": float(latest.get("sopr", 1)),
        "date": latest.get("date"),
    }


async def _fetch_mvrv() -> Optional[dict]:
    """MVRV ratio. >3.7 = market top zone, <1 = bottom/opportunity zone."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/market-indicator/mvrv",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    return {
        "mvrv": float(latest.get("mvrv", 0)),
        "date": latest.get("date"),
    }


async def _fetch_nupl() -> Optional[dict]:
    """NUPL (Net Unrealized Profit/Loss). Network-wide investor sentiment.
    >0.75 = euphoria, 0.5-0.75 = belief, 0.25-0.5 = optimism, 0-0.25 = hope/fear, <0 = capitulation."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/network-indicator/nupl",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    # NUPL field name varies; try common keys
    val = latest.get("nupl") or latest.get("net_unrealized_profit_loss") or 0
    return {"nupl": float(val), "date": latest.get("date")}


async def _fetch_mpi() -> Optional[dict]:
    """MPI (Miner Position Index). >2 = miners selling unusually, <0 = accumulating."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/flow-indicator/mpi",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("mpi") or latest.get("miner_position_index") or 0
    return {"mpi": float(val), "date": latest.get("date")}


async def _fetch_puell_multiple() -> Optional[dict]:
    """Puell Multiple. Miner revenue vs 365-day average. >4 = miner top, <0.5 = miner bottom."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/network-indicator/puell-multiple",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("puell_multiple") or latest.get("puell") or 0
    return {"puell": float(val), "date": latest.get("date")}


async def _fetch_leverage_ratio() -> Optional[dict]:
    """Estimated Leverage Ratio (all exchanges). >0.30 = critical, >0.36 = liquidation flush risk."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/market-indicator/estimated-leverage-ratio",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("estimated_leverage_ratio") or latest.get("elr") or 0
    return {"leverage_ratio": float(val), "date": latest.get("date")}


async def _fetch_realized_price() -> Optional[dict]:
    """Realized Price — average price coins last moved at. Acts as long-term cost basis."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/market-indicator/realized-price",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("realized_price") or 0
    return {"realized_price": float(val), "date": latest.get("date")}


async def _fetch_hash_rate() -> Optional[dict]:
    """BTC network hash rate (EH/s). 7-day change indicates network security trend."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/btc/network-data/hashrate",
        {"window": "day", "from": date_from, "limit": 8},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if len(rows) < 2:
        return None
    latest = float(rows[-1].get("hashrate") or rows[-1].get("hash_rate") or 0)
    oldest = float(rows[0].get("hashrate") or rows[0].get("hash_rate") or 1)
    change_7d = ((latest - oldest) / oldest * 100) if oldest else 0
    return {
        "hash_rate": latest,
        "change_7d_pct": round(change_7d, 2),
        "date": rows[-1].get("date"),
    }


async def _fetch_coinbase_premium() -> Optional[dict]:
    """BTC Coinbase premium index (daily — Pro plan saatlik vermiyor).
    Pozitif = ABD kurumsal alıcı aktif."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/btc/market-data/coinbase-premium-index",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    # Daily endpoint returns coinbase_premium_gap (USD diff) primarily
    val = latest.get("coinbase_premium_gap")
    if val is None:
        val = latest.get("coinbase_premium_index", 0)
    return {
        "coinbase_premium": float(val),
        "ts": latest.get("date"),
    }


# ── Signal interpreter ─────────────────────────────────────────────────────────

def _interpret_signals(snapshot: dict) -> dict:
    """
    Translates raw on-chain numbers into Turkish-language signal labels.
    Returns: { metric → { value_str, signal: 'BULLISH'|'BEARISH'|'NEUTRAL', label_tr } }
    """
    signals: dict[str, Any] = {}

    # Exchange netflow — symbol-aware thresholds (BTC/ETH/XRP ölçekleri farklı)
    nf = snapshot.get("exchange_netflow")
    if nf:
        val = nf["netflow_total"]
        sym = snapshot.get("symbol", "BTC")
        # Kademeli eşikler — Day 26 öğrenmesi: çok geniş eşik gerçek
        # sinyali sessizce skip ediyor. Hafif/şiddetli ayrımı.
        if sym == "BTC":
            t_strong, t_mild = 5000, 500       # BTC
        elif sym == "ETH":
            t_strong, t_mild = 80000, 8000     # ETH (24x BTC ölçeği civarı)
        elif sym == "XRP":
            t_strong, t_mild = 80_000_000, 8_000_000   # XRP supply çok büyük
        else:
            t_strong, t_mild = 5000, 500
        if val > t_strong:
            sig, label = "BEARISH", "🔴 Şiddetli Satış"
        elif val > t_mild:
            sig, label = "BEARISH", "⚠️ Hafif Satış Baskısı"
        elif val < -t_strong:
            sig, label = "BULLISH", "💎 Şiddetli Birikim"
        elif val < -t_mild:
            sig, label = "BULLISH", "🟢 Hafif Birikim"
        else:
            sig, label = "NEUTRAL", "🟡 Dengeli"
        unit = sym
        signals["exchange_netflow"] = {
            "value_str": f"{'+' if val >= 0 else ''}{val:,.0f} {unit}",
            "signal": sig,
            "label_tr": label,
        }

    # Whale ratio
    wr = snapshot.get("whale_ratio")
    if wr:
        val = wr["whale_ratio"]
        if val >= 0.85:
            sig, label = "BEARISH", "🔴 Balina Aktif"
        elif val >= 0.7:
            sig, label = "NEUTRAL", "🟡 İzle"
        else:
            sig, label = "BULLISH", "🟢 Normal"
        signals["whale_ratio"] = {
            "value_str": f"{val:.2f}",
            "signal": sig,
            "label_tr": label,
        }

    # Miner pressure
    mr = snapshot.get("miner_reserve")
    if mr:
        chg = mr["change_7d_pct"]
        if chg < -2:
            sig, label = "BEARISH", "🔴 Madenci Satıyor"
        elif chg < 0:
            sig, label = "NEUTRAL", "🟡 Hafif Baskı"
        else:
            sig, label = "BULLISH", "🟢 Stabil"
        signals["miner_reserve"] = {
            "value_str": f"{chg:+.1f}% (7G)",
            "signal": sig,
            "label_tr": label,
        }

    # Stablecoin inflow
    si = snapshot.get("stablecoin_inflow")
    if si:
        val = si["inflow_total"]
        if val > 500_000_000:
            sig, label = "BULLISH", "🟢 Alım Gücü Geliyor"
        elif val > 100_000_000:
            sig, label = "NEUTRAL", "🟡 Orta Giriş"
        else:
            sig, label = "BEARISH", "🔴 Düşük Giriş"
        m = val / 1_000_000
        signals["stablecoin_inflow"] = {
            "value_str": f"+{m:,.0f}M USDT",
            "signal": sig,
            "label_tr": label,
        }

    # Funding rates
    fr = snapshot.get("funding_rates")
    if fr:
        avg = fr["avg_24h"]
        if avg > 0.001:
            sig, label = "BEARISH", "⚠️ Aşırı Long"
        elif avg < -0.001:
            sig, label = "BULLISH", "💡 Short Sıkışması"
        else:
            sig, label = "NEUTRAL", "🟡 Dengeli"
        signals["funding_rates"] = {
            "value_str": f"{avg*100:+.4f}% (ort 24s)",
            "signal": sig,
            "label_tr": label,
        }

    # Open interest
    oi = snapshot.get("open_interest")
    if oi:
        chg = oi["change_pct"]
        val_b = oi["open_interest"] / 1e9
        if chg > 5:
            sig, label = "NEUTRAL", "⚠️ Kaldıraç Artıyor"
        elif chg < -5:
            sig, label = "NEUTRAL", "🟡 Azalıyor"
        else:
            sig, label = "NEUTRAL", "🟡 Stabil"
        signals["open_interest"] = {
            "value_str": f"${val_b:.1f}B ({chg:+.1f}%)",
            "signal": sig,
            "label_tr": label,
        }

    # SOPR
    sopr = snapshot.get("sopr")
    if sopr:
        val = sopr["sopr"]
        if val > 1.02:
            sig, label = "NEUTRAL", "💰 Kar Realizasyonu"
        elif val < 0.98:
            sig, label = "BULLISH", "🟢 Zararına Satış (Dip)"
        else:
            sig, label = "NEUTRAL", "🟡 Başabaş"
        signals["sopr"] = {
            "value_str": f"{val:.4f}",
            "signal": sig,
            "label_tr": label,
        }

    # Coinbase Premium
    cb = snapshot.get("coinbase_premium")
    if cb:
        v = cb["coinbase_premium"]
        if v > 5:
            sig, label = "BULLISH", "🟢 ABD Kurumsal Alım"
        elif v < -5:
            sig, label = "BEARISH", "🔴 ABD Kurumsal Satış"
        else:
            sig, label = "NEUTRAL", "🟡 Nötr"
        signals["coinbase_premium"] = {
            "value_str": f"{v:+.2f}",
            "signal": sig,
            "label_tr": label,
        }

    # MVRV
    mvrv = snapshot.get("mvrv")
    if mvrv:
        v = mvrv["mvrv"]
        if v > 3.7:
            sig, label = "BEARISH", "⚠️ Tarihsel Tepe Bölgesi"
        elif v > 2.4:
            sig, label = "NEUTRAL", "🟡 Aşırı Değerli"
        elif v < 1.0:
            sig, label = "BULLISH", "💎 Tarihsel Dip Bölgesi"
        elif v < 1.5:
            sig, label = "BULLISH", "🟢 Adil-altı"
        else:
            sig, label = "NEUTRAL", "🟡 Adil Değer"
        signals["mvrv"] = {
            "value_str": f"{v:.2f}",
            "signal": sig,
            "label_tr": label,
        }

    # NUPL
    nupl = snapshot.get("nupl")
    if nupl:
        v = nupl["nupl"]
        if v > 0.75:
            sig, label = "BEARISH", "⚠️ Aşırı Coşku (Tepe)"
        elif v > 0.5:
            sig, label = "NEUTRAL", "🟡 İyimserlik"
        elif v < 0:
            sig, label = "BULLISH", "💎 Kapitülasyon (Dip)"
        elif v < 0.25:
            sig, label = "BULLISH", "🟢 Korku/Umut"
        else:
            sig, label = "NEUTRAL", "🟡 İnanç"
        signals["nupl"] = {
            "value_str": f"{v:.3f}",
            "signal": sig,
            "label_tr": label,
        }

    # MPI (Miner Position Index)
    mpi = snapshot.get("mpi")
    if mpi:
        v = mpi["mpi"]
        if v > 2:
            sig, label = "BEARISH", "🔴 Madenci Aşırı Satış"
        elif v > 0.5:
            sig, label = "NEUTRAL", "🟡 Hafif Madenci Satışı"
        elif v < -0.5:
            sig, label = "BULLISH", "🟢 Madenci Birikim"
        else:
            sig, label = "BULLISH", "🟢 Madenci Güveni"
        signals["mpi"] = {
            "value_str": f"{v:+.2f}",
            "signal": sig,
            "label_tr": label,
        }

    # Puell Multiple
    puell = snapshot.get("puell")
    if puell:
        v = puell["puell"]
        if v > 4:
            sig, label = "BEARISH", "⚠️ Madenci Aşırı Karlı (Sat zamanı)"
        elif v < 0.5:
            sig, label = "BULLISH", "💎 Madenci Kapitülasyon (Dip)"
        elif v < 1.0:
            sig, label = "BULLISH", "🟢 Madenci Düşük Karda"
        else:
            sig, label = "NEUTRAL", "🟡 Normal Madenci Karı"
        signals["puell"] = {
            "value_str": f"{v:.2f}",
            "signal": sig,
            "label_tr": label,
        }

    # Estimated Leverage Ratio
    lev = snapshot.get("leverage_ratio")
    if lev:
        v = lev["leverage_ratio"]
        if v > 0.36:
            sig, label = "BEARISH", "🔴 Kritik Kaldıraç (Tasfiye Riski)"
        elif v > 0.30:
            sig, label = "NEUTRAL", "🟠 Yüksek Kaldıraç"
        elif v < 0.20:
            sig, label = "BULLISH", "🟢 Düşük Risk"
        else:
            sig, label = "NEUTRAL", "🟡 Normal"
        signals["leverage_ratio"] = {
            "value_str": f"{v:.3f}",
            "signal": sig,
            "label_tr": label,
        }

    # Realized Price (passive — context only, no signal weight)
    rp = snapshot.get("realized_price")
    if rp:
        v = rp["realized_price"]
        signals["realized_price"] = {
            "value_str": f"${v:,.0f}",
            "signal": "NEUTRAL",
            "label_tr": "📐 Piyasa Ortalama Maliyeti",
        }

    # ETH-specific: exchange supply ratio (whale-ratio benzeri)
    esr = snapshot.get("eth_supply_ratio")
    if esr:
        v = esr["supply_ratio"]
        # ETH supply on exchanges typically 0.10-0.20 range; lower = accumulating
        if v < 0.10:
            sig, label = "BULLISH", "🟢 Borsa Arzı Düşük"
        elif v < 0.13:
            sig, label = "BULLISH", "🟢 Sağlıklı"
        elif v > 0.18:
            sig, label = "BEARISH", "⚠️ Borsa Arzı Yüksek"
        else:
            sig, label = "NEUTRAL", "🟡 Normal"
        signals["eth_supply_ratio"] = {
            "value_str": f"{v:.3f}",
            "signal": sig,
            "label_tr": label,
        }

    # ETH active addresses
    aa = snapshot.get("eth_active_addresses")
    if aa:
        chg = aa["change_7d_pct"]
        if chg > 5:
            sig, label = "BULLISH", "🟢 Ağ Kullanımı Artıyor"
        elif chg < -5:
            sig, label = "BEARISH", "🔴 Ağ Kullanımı Düşüyor"
        else:
            sig, label = "NEUTRAL", "🟡 Stabil"
        signals["eth_active_addresses"] = {
            "value_str": f"{chg:+.1f}% (7G)",
            "signal": sig,
            "label_tr": label,
        }

    # Hash Rate (passive — long-term security indicator). CQ returns raw
    # network hash count which doesn't cleanly map to EH/s without extra
    # math; we display only the 7-day delta which is what actually matters
    # for the "is the network getting stronger?" read.
    hr = snapshot.get("hash_rate")
    if hr:
        chg = hr["change_7d_pct"]
        if chg > 5:
            sig, label = "BULLISH", "🟢 Ağ Güçleniyor"
        elif chg < -5:
            sig, label = "NEUTRAL", "🟡 Hash Düşüşü"
        else:
            sig, label = "NEUTRAL", "🟡 Stabil"
        signals["hash_rate"] = {
            "value_str": f"{chg:+.1f}% (7G)",
            "signal": sig,
            "label_tr": label,
        }

    # XRP — Liquidations (USD)
    liq = snapshot.get("xrp_liquidations")
    if liq:
        total = liq["total_usd"]
        long_v = liq["long_usd"]
        short_v = liq["short_usd"]
        # Asymmetry → güçlü sinyal: sadece bir taraf temizleniyor
        if total > 5_000_000:
            if long_v > short_v * 2:
                sig, label = "BULLISH", "🟢 Long Tasfiyesi (Dip Sinyali)"
            elif short_v > long_v * 2:
                sig, label = "BEARISH", "🔴 Short Sıkışması Sonrası"
            else:
                sig, label = "NEUTRAL", "⚠️ Çift Yönlü Tasfiye"
        elif total > 500_000:
            sig, label = "NEUTRAL", "🟡 Orta Tasfiye"
        else:
            sig, label = "BULLISH", "🟢 Sakin Türev"
        signals["xrp_liquidations"] = {
            "value_str": f"${total/1000:,.0f}K (L:{long_v/1000:.0f}K · S:{short_v/1000:.0f}K)",
            "signal": sig,
            "label_tr": label,
        }

    # XRP — Taker Buy/Sell oranı
    taker = snapshot.get("xrp_taker_buy_sell")
    if taker:
        bratio = taker["buy_ratio"]
        if bratio > 0.55:
            sig, label = "BULLISH", "🟢 Alıcı Baskın"
        elif bratio > 0.52:
            sig, label = "BULLISH", "🟢 Hafif Alıcı"
        elif bratio < 0.45:
            sig, label = "BEARISH", "🔴 Satıcı Baskın"
        elif bratio < 0.48:
            sig, label = "BEARISH", "⚠️ Hafif Satıcı"
        else:
            sig, label = "NEUTRAL", "🟡 Dengeli"
        signals["xrp_taker_buy_sell"] = {
            "value_str": f"%{bratio*100:.1f} alıcı",
            "signal": sig,
            "label_tr": label,
        }

    # XRP — Supply ratio (borsalardaki XRP / toplam supply)
    xsr = snapshot.get("xrp_supply_ratio")
    if xsr:
        v = xsr["supply_ratio"]
        if v < 0.10:
            sig, label = "BULLISH", "🟢 Borsa Stoku Düşük"
        elif v < 0.13:
            sig, label = "BULLISH", "🟢 Sağlıklı"
        elif v > 0.18:
            sig, label = "BEARISH", "⚠️ Borsa Stoku Yüksek"
        else:
            sig, label = "NEUTRAL", "🟡 Normal"
        signals["xrp_supply_ratio"] = {
            "value_str": f"{v:.3f}",
            "signal": sig,
            "label_tr": label,
        }

    # XRP — NVT (Network Value to Transactions)
    nvt = snapshot.get("xrp_nvt")
    if nvt:
        v = nvt["nvt"]
        if v > 200:
            sig, label = "BEARISH", "⚠️ Aşırı Değerli (Düşük Kullanım)"
        elif v > 100:
            sig, label = "NEUTRAL", "🟡 Yüksek Değerleme"
        elif v < 30:
            sig, label = "BULLISH", "💎 Çok Aktif Ağ"
        elif v < 60:
            sig, label = "BULLISH", "🟢 Sağlıklı Kullanım"
        else:
            sig, label = "NEUTRAL", "🟡 Normal"
        signals["xrp_nvt"] = {
            "value_str": f"{v:.1f}",
            "signal": sig,
            "label_tr": label,
        }

    # XRP — Transaction count (7G değişim)
    txc = snapshot.get("xrp_tx_count")
    if txc:
        chg = txc["change_7d_pct"]
        if chg > 10:
            sig, label = "BULLISH", "🟢 Ağ Kullanımı Artıyor"
        elif chg > 0:
            sig, label = "BULLISH", "🟢 Hafif Artış"
        elif chg < -10:
            sig, label = "BEARISH", "🔴 Ağ Kullanımı Düşüyor"
        else:
            sig, label = "NEUTRAL", "🟡 Stabil"
        signals["xrp_tx_count"] = {
            "value_str": f"{chg:+.1f}% (7G)",
            "signal": sig,
            "label_tr": label,
        }

    # ── Axiom Skor: weighted 0-100 composite ─────────────────────────────────
    # Sinyal başına ±max ağırlık. Symbol'e bakarak sadece mevcut sinyaller
    # skora girer (max_total dinamik hesaplanır).
    # BTC: 16 sinyal · ETH: 7 sinyal · XRP: 8 sinyal.
    weights = {
        "exchange_netflow":     25,  # en yüksek aksiyonlanabilir sinyal
        "whale_ratio":          20,
        "mpi":                  18,
        "stablecoin_inflow":    15,
        "leverage_ratio":       15,
        "funding_rates":        13,
        "nupl":                 12,
        "sopr":                 10,
        "coinbase_premium":      8,
        # ETH-specific
        "eth_supply_ratio":     20,  # whale_ratio'nun ETH karşılığı
        "eth_active_addresses": 10,  # ağ canlılığı
        # XRP-specific
        "xrp_liquidations":     15,  # tasfiye asimetrisi güçlü sinyal
        "xrp_taker_buy_sell":   18,  # spot tarafındaki agresörlük
        "xrp_supply_ratio":     18,  # borsa arz dengesi
        "xrp_nvt":              12,  # değerleme katsayısı
        "xrp_tx_count":         10,  # ağ büyümesi
    }

    breakdown = []
    signed_total = 0.0
    max_total = 0.0
    for key, max_w in weights.items():
        s = signals.get(key)
        if not s:
            continue
        max_total += max_w
        if s["signal"] == "BULLISH":
            contrib = max_w
        elif s["signal"] == "BEARISH":
            contrib = -max_w
        else:
            contrib = 0
        signed_total += contrib
        breakdown.append({
            "metric": key,
            "label_tr": s["label_tr"],
            "value_str": s["value_str"],
            "weight": max_w,
            "contribution": contrib,
            "signal": s["signal"],
        })

    if max_total == 0:
        axiom_score = None
        score_zone = "UNKNOWN"
        score_zone_tr = "❓ Veri Yok"
        score_summary = "On-chain veri henüz oluşmadı."
    else:
        # Normalize signed_total ∈ [-max_total, +max_total] → [0, 100]
        axiom_score = round(50 + (signed_total / max_total) * 50, 1)
        axiom_score = max(0, min(100, axiom_score))

        if axiom_score >= 86:
            score_zone, score_zone_tr = "OPPORTUNITY", "💎 Fırsat"
        elif axiom_score >= 71:
            score_zone, score_zone_tr = "SAFE", "🟢 Güvenli"
        elif axiom_score >= 51:
            score_zone, score_zone_tr = "CAUTION", "🟡 Dikkatli"
        elif axiom_score >= 31:
            score_zone, score_zone_tr = "RISKY", "🟠 Riskli"
        else:
            score_zone, score_zone_tr = "DANGER", "🔴 Tehlikeli"

        # Build human summary from top contributors
        positives = sorted([b for b in breakdown if b["contribution"] > 0],
                           key=lambda x: -x["contribution"])[:3]
        negatives = sorted([b for b in breakdown if b["contribution"] < 0],
                           key=lambda x: x["contribution"])[:3]
        parts = []
        if positives:
            parts.append("Güç: " + ", ".join(p["label_tr"] for p in positives))
        if negatives:
            parts.append("Baskı: " + ", ".join(n["label_tr"] for n in negatives))
        score_summary = " · ".join(parts) if parts else "Tüm sinyaller nötr bölgede."

    # Overall signal (legacy — count-based, korunuyor backward compatibility için)
    bearish = sum(1 for s in signals.values() if s["signal"] == "BEARISH")
    bullish = sum(1 for s in signals.values() if s["signal"] == "BULLISH")
    total = len(signals)
    if total == 0:
        overall = "UNKNOWN"
        overall_tr = "❓ Veri Yok"
    elif bearish > total * 0.5:
        overall = "BEARISH"
        overall_tr = "⚠️ Satıcı Üstünlüğü"
    elif bullish > total * 0.5:
        overall = "BULLISH"
        overall_tr = "🟢 Alıcı Üstünlüğü"
    else:
        overall = "MIXED"
        overall_tr = "🟡 Karışık Sinyal"

    return {
        "signals": signals,
        "overall": overall,
        "overall_tr": overall_tr,
        "axiom_score": axiom_score,
        "score_zone": score_zone,
        "score_zone_tr": score_zone_tr,
        "score_summary": score_summary,
        "score_breakdown": breakdown,  # premium-only render
    }


# ── Public API ─────────────────────────────────────────────────────────────────

async def _build_btc_snapshot() -> dict:
    """16 paralel BTC metrik fetch — Pro plan'ın tamamından yararlanıyor."""
    results = await asyncio.gather(
        _fetch_exchange_netflow(),
        _fetch_whale_ratio(),
        _fetch_miner_outflow(),
        _fetch_miner_reserve(),
        _fetch_stablecoin_inflow(),
        _fetch_funding_rates(),
        _fetch_open_interest(),
        _fetch_sopr(),
        _fetch_coinbase_premium(),
        _fetch_mvrv(),
        _fetch_nupl(),
        _fetch_mpi(),
        _fetch_puell_multiple(),
        _fetch_leverage_ratio(),
        _fetch_realized_price(),
        _fetch_hash_rate(),
    )
    (
        netflow, whale_ratio, miner_outflow, miner_reserve,
        stablecoin_inflow, funding_rates, open_interest, sopr, cb_premium,
        mvrv, nupl, mpi, puell, leverage_ratio, realized_price, hash_rate,
    ) = results
    return {
        "symbol": "BTC",
        "exchange_netflow": netflow,
        "whale_ratio": whale_ratio,
        "miner_outflow": miner_outflow,
        "miner_reserve": miner_reserve,
        "stablecoin_inflow": stablecoin_inflow,
        "funding_rates": funding_rates,
        "open_interest": open_interest,
        "sopr": sopr,
        "coinbase_premium": cb_premium,
        "mvrv": mvrv,
        "nupl": nupl,
        "mpi": mpi,
        "puell": puell,
        "leverage_ratio": leverage_ratio,
        "realized_price": realized_price,
        "hash_rate": hash_rate,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "_snapshot_full": True,
    }


async def _build_eth_snapshot() -> dict:
    """ETH snapshot — Pro plan'da 7 sinyal aktif. PoS olduğu için BTC'nin
    miner/MVRV/SOPR'u yok, ama funding + coinbase premium çalışıyor."""
    results = await asyncio.gather(
        _fetch_eth_netflow(),
        _fetch_eth_exchange_supply_ratio(),
        _fetch_eth_leverage_ratio(),
        _fetch_eth_open_interest(),
        _fetch_eth_active_addresses(),
        _fetch_eth_funding_rates(),
        _fetch_eth_coinbase_premium(),
    )
    netflow, supply_ratio, leverage, oi, active_addr, funding, cb_premium = results
    return {
        "symbol": "ETH",
        "exchange_netflow": netflow,
        "eth_supply_ratio": supply_ratio,
        "leverage_ratio": leverage,
        "open_interest": oi,
        "eth_active_addresses": active_addr,
        "funding_rates": funding,
        "coinbase_premium": cb_premium,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "_snapshot_full": True,
    }


async def get_onchain_snapshot(symbol: str = "BTC") -> dict:
    """
    Returns on-chain snapshot for BTC (16 sinyal full), ETH (7 sinyal),
    or XRP (8 sinyal full). Diğer semboller error döner.
    """
    symbol = (symbol or "BTC").upper()
    if not _is_configured():
        return {"error": "cryptoquant_not_configured", "symbol": symbol}

    if symbol not in ("BTC", "ETH", "XRP"):
        return {"error": "symbol_not_supported", "symbol": symbol,
                "supported": ["BTC", "ETH", "XRP"]}

    # Try cache first
    cached = await _cache_get("snapshot", symbol, "day")
    if cached and cached.get("_snapshot_full"):
        return cached

    if symbol == "BTC":
        raw = await _build_btc_snapshot()
    elif symbol == "ETH":
        raw = await _build_eth_snapshot()
    else:  # XRP
        raw = await _build_xrp_snapshot()
    interpreted = _interpret_signals(raw)
    snapshot = {**raw, **interpreted}

    await _cache_set("snapshot", symbol, "day", snapshot, _TTL_EXCHANGE_FLOW)
    return snapshot


# ── ETH-specific fetchers (Pro plan kapsamı: 6 sinyal — whale ratio,
# MVRV, SOPR, funding, coinbase premium, MPI/Puell ETH'de yok) ───────────────

async def _fetch_eth_netflow() -> Optional[dict]:
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/eth/exchange-flows/netflow",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    return {
        "netflow_total": float(latest.get("netflow_total", 0)),
        "inflow_total":  float(latest.get("inflow_total", 0)),
        "outflow_total": float(latest.get("outflow_total", 0)),
        "date": latest.get("date", yesterday),
    }


async def _fetch_eth_exchange_supply_ratio() -> Optional[dict]:
    """ETH'de whale_ratio yok ama exchange-supply-ratio benzer sinyal verir
    (borsalardaki ETH supply'in toplam supply'a oranı)."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/eth/flow-indicator/exchange-supply-ratio",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("exchange_supply_ratio") or latest.get("supply_ratio") or 0
    return {"supply_ratio": float(val), "date": latest.get("date")}


async def _fetch_eth_leverage_ratio() -> Optional[dict]:
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/eth/market-indicator/estimated-leverage-ratio",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("estimated_leverage_ratio") or latest.get("elr") or 0
    return {"leverage_ratio": float(val), "date": latest.get("date")}


async def _fetch_eth_open_interest() -> Optional[dict]:
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/eth/market-data/open-interest",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else latest
    oi = float(latest.get("open_interest", 0))
    oi_prev = float(prev.get("open_interest", 1))
    change_pct = ((oi - oi_prev) / oi_prev * 100) if oi_prev else 0
    return {
        "open_interest": oi,
        "change_pct": round(change_pct, 2),
        "date": latest.get("date"),
    }


async def _fetch_eth_active_addresses() -> Optional[dict]:
    """ETH ağ canlılığı — günlük aktif cüzdan sayısı, 7G değişimi."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/eth/network-data/addresses-count",
        {"window": "day", "from": date_from, "limit": 8},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if len(rows) < 2:
        return None
    latest = float(rows[-1].get("addresses_count") or 0)
    oldest = float(rows[0].get("addresses_count") or 1)
    change = ((latest - oldest) / oldest * 100) if oldest else 0
    return {
        "active_addresses": latest,
        "change_7d_pct": round(change, 2),
        "date": rows[-1].get("date"),
    }


async def _fetch_eth_funding_rates() -> Optional[dict]:
    """ETH funding rates (Binance, daily — Pro plan saatlik vermiyor)."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/eth/market-data/funding-rates",
        {"exchange": "binance", "window": "day", "from": date_from, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    values = [float(r.get("funding_rates", 0)) for r in rows if r.get("funding_rates") is not None]
    if not values:
        return None
    return {
        "latest": values[-1],
        "avg_24h": values[-1],
        "ts": rows[-1].get("date"),
    }


# ── XRP fetchers (Pro plan'da 8 sinyal — derivatives ağırlıklı) ─────────────

async def _fetch_xrp_funding_rates() -> Optional[dict]:
    """XRP funding rates (Binance, daily — Pro plan saatlik vermiyor)."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/xrp/market-data/funding-rates",
        {"exchange": "binance", "window": "day", "from": date_from, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    values = [float(r.get("funding_rates", 0)) for r in rows if r.get("funding_rates") is not None]
    if not values:
        return None
    return {
        "latest": values[-1],
        "avg_24h": values[-1],
        "ts": rows[-1].get("date"),
    }


async def _fetch_xrp_open_interest() -> Optional[dict]:
    """XRP açık pozisyon (USD), 1g değişimi."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/xrp/market-data/open-interest",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else latest
    oi = float(latest.get("open_interest", 0))
    oi_prev = float(prev.get("open_interest", 1))
    change_pct = ((oi - oi_prev) / oi_prev * 100) if oi_prev else 0
    return {
        "open_interest": oi,
        "change_pct": round(change_pct, 2),
        "date": latest.get("date"),
    }


async def _fetch_xrp_liquidations() -> Optional[dict]:
    """XRP tasfiyeler (long + short USD)."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/xrp/market-data/liquidations",
        {"exchange": "binance", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    long_usd = float(latest.get("long_liquidations_usd", 0))
    short_usd = float(latest.get("short_liquidations_usd", 0))
    return {
        "long_usd": long_usd,
        "short_usd": short_usd,
        "total_usd": long_usd + short_usd,
        "date": latest.get("date"),
    }


async def _fetch_xrp_taker_buy_sell() -> Optional[dict]:
    """XRP taker buy/sell oranı (Binance). >0.55 = alıcı baskın."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/xrp/market-data/taker-buy-sell-stats",
        {"exchange": "binance", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    return {
        "buy_ratio": float(latest.get("taker_buy_ratio", 0)),
        "sell_ratio": float(latest.get("taker_sell_ratio", 0)),
        "buy_volume": float(latest.get("taker_buy_volume", 0)),
        "date": latest.get("date"),
    }


async def _fetch_xrp_leverage_ratio() -> Optional[dict]:
    """XRP tahmini kaldıraç oranı (Binance)."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/xrp/market-data/estimated-leverage-ratio",
        {"exchange": "binance", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("estimated_leverage_ratio") or latest.get("elr") or 0
    return {"leverage_ratio": float(val), "date": latest.get("date")}


async def _fetch_xrp_supply_ratio() -> Optional[dict]:
    """XRP borsa arz oranı — düşük = biriktirilme."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/xrp/flow-indicator/exchange-supply-ratio",
        {"exchange": "all_exchange", "window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("exchange_supply_ratio") or latest.get("supply_ratio") or 0
    return {"supply_ratio": float(val), "date": latest.get("date")}


async def _fetch_xrp_nvt() -> Optional[dict]:
    """XRP NVT — ağ değeri / işlem hacmi. Yüksek = balon, düşük = ucuz."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/xrp/network-indicator/nvt",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    return {"nvt": float(latest.get("nvt", 0)), "date": latest.get("date")}


async def _fetch_xrp_tx_count() -> Optional[dict]:
    """XRP günlük işlem sayısı, 7G değişimi."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/xrp/network-data/transactions-count",
        {"window": "day", "from": date_from, "limit": 8},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if len(rows) < 2:
        return None
    latest = float(rows[-1].get("total_transactions_count") or rows[-1].get("transactions_count") or 0)
    oldest = float(rows[0].get("total_transactions_count") or rows[0].get("transactions_count") or 1)
    change = ((latest - oldest) / oldest * 100) if oldest else 0
    return {
        "tx_count": latest,
        "change_7d_pct": round(change, 2),
        "date": rows[-1].get("date"),
    }


async def _build_xrp_snapshot() -> dict:
    """XRP full snapshot — 8 sinyal (derivatives + network).
    BTC seviyesinde Axiom Skoru üretir."""
    results = await asyncio.gather(
        _fetch_xrp_funding_rates(),
        _fetch_xrp_open_interest(),
        _fetch_xrp_liquidations(),
        _fetch_xrp_taker_buy_sell(),
        _fetch_xrp_leverage_ratio(),
        _fetch_xrp_supply_ratio(),
        _fetch_xrp_nvt(),
        _fetch_xrp_tx_count(),
    )
    funding, oi, liq, taker, leverage, supply_ratio, nvt, tx_count = results
    return {
        "symbol": "XRP",
        "funding_rates": funding,
        "open_interest": oi,
        "xrp_liquidations": liq,
        "xrp_taker_buy_sell": taker,
        "leverage_ratio": leverage,
        "xrp_supply_ratio": supply_ratio,
        "xrp_nvt": nvt,
        "xrp_tx_count": tx_count,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "_snapshot_full": True,
    }


# ── ETH-specific (devam) ─────────────────────────────────────────────────────

async def _fetch_eth_coinbase_premium() -> Optional[dict]:
    """ETH Coinbase premium index (daily — Pro plan saatlik vermiyor).
    Pozitif = ABD kurumsal ETH alımı aktif."""
    yesterday = _yesterday_str()
    raw = await _cq_get(
        "/eth/market-data/coinbase-premium-index",
        {"window": "day", "from": yesterday, "limit": 3},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("coinbase_premium_gap")
    if val is None:
        val = latest.get("coinbase_premium_index", 0)
    return {
        "coinbase_premium": float(val),
        "ts": latest.get("date"),
    }


async def get_btc_spot_price() -> Optional[float]:
    """Latest BTC close price via CryptoQuant /btc/market-data/price-ohlcv (daily).
    Pro plan saatlik vermiyor. ETF flow scheduler + spot price fallback için."""
    if not _is_configured():
        return None
    raw = await _cq_get(
        "/btc/market-data/price-ohlcv",
        {
            "market": "spot",
            "exchange": "binance",
            "symbol": "btc_usdt",
            "window": "day",
            "limit": 1,
        },
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    val = latest.get("close") or latest.get("price")
    try:
        return float(val) if val else None
    except (TypeError, ValueError):
        return None


async def refresh_all_metrics() -> None:
    """Force-refresh all metrics for BTC + ETH. Called by scheduler."""
    logger.info("CryptoQuant: refreshing BTC + ETH snapshots...")
    try:
        sql = text("DELETE FROM cryptoquant_cache WHERE metric_key = 'snapshot'")
        async with engine.begin() as conn:
            await conn.execute(sql)
        # Sequential to avoid simultaneous CryptoQuant rate-limit hits.
        await get_onchain_snapshot("BTC")
        await get_onchain_snapshot("ETH")
        logger.info("CryptoQuant: refresh complete (BTC + ETH)")
    except Exception as e:
        logger.error(f"CryptoQuant refresh error: {e}")
