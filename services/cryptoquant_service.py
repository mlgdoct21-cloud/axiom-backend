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
    return {
        "whale_ratio": float(latest.get("whale_ratio", 0)),
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
    """BTC funding rates (Binance, last 24h hourly → avg + latest)."""
    date_from = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%dT%H:%M:%S")
    raw = await _cq_get(
        "/btc/market-data/funding-rates",
        {"exchange": "binance", "window": "hour", "from": date_from, "limit": 24},
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
        "avg_24h": sum(values) / len(values),
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
    """Coinbase premium index (latest hourly). Positive = US institutional buyers active."""
    date_from = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y%m%dT%H:%M:%S")
    raw = await _cq_get(
        "/btc/market-data/coinbase-premium-index",
        {"window": "hour", "from": date_from, "limit": 4},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    latest = rows[-1]
    return {
        "coinbase_premium": float(latest.get("coinbase_premium_index", 0)),
        "ts": latest.get("date"),
    }


# ── Signal interpreter ─────────────────────────────────────────────────────────

def _interpret_signals(snapshot: dict) -> dict:
    """
    Translates raw on-chain numbers into Turkish-language signal labels.
    Returns: { metric → { value_str, signal: 'BULLISH'|'BEARISH'|'NEUTRAL', label_tr } }
    """
    signals: dict[str, Any] = {}

    # Exchange netflow
    nf = snapshot.get("exchange_netflow")
    if nf:
        val = nf["netflow_total"]
        if val > 5000:
            sig, label = "BEARISH", "⚠️ Satış Baskısı"
        elif val < -5000:
            sig, label = "BULLISH", "🟢 Birikim"
        else:
            sig, label = "NEUTRAL", "🟡 Nötr"
        signals["exchange_netflow"] = {
            "value_str": f"{'+' if val >= 0 else ''}{val:,.0f} BTC",
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

    # Hash Rate (passive — long-term security indicator)
    hr = snapshot.get("hash_rate")
    if hr:
        chg = hr["change_7d_pct"]
        eh = hr["hash_rate"] / 1e18 if hr["hash_rate"] > 1e15 else hr["hash_rate"]
        if chg > 5:
            sig, label = "BULLISH", "🟢 Ağ Güçleniyor"
        elif chg < -5:
            sig, label = "NEUTRAL", "🟡 Hash Düşüşü"
        else:
            sig, label = "NEUTRAL", "🟡 Stabil"
        signals["hash_rate"] = {
            "value_str": f"{eh:.0f} EH/s ({chg:+.1f}%)",
            "signal": sig,
            "label_tr": label,
        }

    # ── Axiom Skor: weighted 0-100 composite ─────────────────────────────────
    # Sinyal başına ±max ağırlık. Toplam max range 136 (-68 ile +68).
    # Skor = 50 + (signed_total / 136) * 50, clamp [0, 100].
    weights = {
        "exchange_netflow":  25,  # en yüksek aksiyonlanabilir sinyal
        "whale_ratio":       20,
        "mpi":               18,
        "stablecoin_inflow": 15,
        "leverage_ratio":    15,
        "funding_rates":     13,
        "nupl":              12,
        "sopr":              10,
        "coinbase_premium":   8,
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

async def get_onchain_snapshot(symbol: str = "BTC") -> dict:
    """
    Returns full on-chain snapshot for the given symbol (currently BTC only).
    Checks cache first; fetches from API if stale.
    """
    if not _is_configured():
        return {"error": "cryptoquant_not_configured", "symbol": symbol}

    # Try cache first (use exchange_netflow as proxy for freshness)
    cached = await _cache_get("exchange_netflow", symbol, "day")
    if cached and cached.get("_snapshot_full"):
        return cached

    # Fetch all metrics in parallel (16 endpoints — gather is fan-out limited
    # only by aiohttp's connection pool; CryptoQuant tolerates this volume)
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

    raw = {
        "symbol": symbol,
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

    interpreted = _interpret_signals(raw)
    snapshot = {**raw, **interpreted}

    # Cache with exchange_netflow TTL (shortest of exchange-flow metrics = 4h)
    await _cache_set("exchange_netflow", symbol, "day", snapshot, _TTL_EXCHANGE_FLOW)

    return snapshot


async def refresh_all_metrics() -> None:
    """Force-refresh all metrics. Called by scheduler."""
    logger.info("CryptoQuant: refreshing all metrics...")
    try:
        # Bust cache by deleting the proxy key
        sql = text("""
            DELETE FROM cryptoquant_cache
            WHERE metric_key = 'exchange_netflow' AND symbol = 'BTC'
        """)
        async with engine.begin() as conn:
            await conn.execute(sql)
        await get_onchain_snapshot("BTC")
        logger.info("CryptoQuant: refresh complete")
    except Exception as e:
        logger.error(f"CryptoQuant refresh error: {e}")
