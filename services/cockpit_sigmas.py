"""
Cockpit Sigma Service — Faz A.3 (deferred backend) implementation.

Dashboard'daki Kokpit chip'leri (Netflow, Funding, BTC fiyat) için tarihsel
σ-sapma (z-score) hesaplar. AXIOM brand promise: "ham veri = gürültü; her
sayı tarihsel bağlamda göster".

Hesap mantığı (90 günlük rolling window):
    z = (current - mean_90d) / stddev_90d

Sonuç:
    |z| >= 2.0 → istatistiksel olarak anlamlı sapma (üst veya alt %2.5)
    |z| >= 1.5 → dikkat çekici sapma
    |z| <  1.0 → normal bant

Veri kaynakları:
    netflow → CryptoQuant /btc/exchange-flows/netflow (90 gün)
    funding → CryptoQuant /btc/market-data/funding-rates (90 gün)
    btc_price → CoinGecko /coins/bitcoin/market_chart (90 gün)

Cache:
    cryptoquant_cache postgres tablosu, window='sigma_90d', TTL 1 saat.
    Z-score değerleri saatlik refresh — uzun-vade istatistik 60 dakikada
    anlamlı değişmez ama "demo" rozetinden çıkıp "canlı" rozetine geçmesi
    için yeterli sıklık.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from services.cryptoquant_service import _cq_get, _cache_get, _cache_set

logger = logging.getLogger("cockpit_sigmas")

_SIGMA_WINDOW = "sigma_90d"
_SIGMA_TTL = timedelta(hours=1)
_LOOKBACK_DAYS = 90


def _zscore(values: list[float], current: Optional[float] = None) -> Optional[float]:
    """
    Listenin son elemanı (veya verilen current) için z-score döner.
    En az 30 örnek yoksa None döner (yetersiz istatistiksel güç).
    """
    if not values or len(values) < 30:
        return None
    sample = values[:-1] if current is not None else values[:-1]
    target = current if current is not None else values[-1]
    try:
        mean = statistics.fmean(sample)
        # stdev örnek (N-1) — popülasyon değil
        std = statistics.stdev(sample)
        if std == 0 or std != std:  # std=0 veya NaN
            return None
        return (target - mean) / std
    except statistics.StatisticsError as e:
        logger.warning(f"zscore stats error: {e}")
        return None


# ──────────────────────────────────────────────────────────────────
# Netflow z-score
# ──────────────────────────────────────────────────────────────────

async def _fetch_netflow_history(days: int = _LOOKBACK_DAYS) -> Optional[list[float]]:
    """Son N gün BTC exchange netflow_total değerleri (kronolojik sırayla)."""
    from_date = (datetime.now(timezone.utc) - timedelta(days=days + 5)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/btc/exchange-flows/netflow",
        {"exchange": "all_exchange", "window": "day", "from": from_date, "limit": days + 5},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    # date alanına göre sırala (eski → yeni); CryptoQuant genelde böyle ama emin ol
    rows = sorted(rows, key=lambda r: r.get("date", ""))
    values = [
        float(r["netflow_total"])
        for r in rows
        if r.get("netflow_total") is not None
    ]
    return values[-days:] if len(values) > days else values


async def compute_netflow_zscore() -> Optional[dict]:
    """
    Netflow için 90g z-score.
    Cache: cryptoquant_cache, metric='cockpit_netflow_sigma', sym='BTC', win='sigma_90d'.
    """
    cached = await _cache_get("cockpit_netflow_sigma", "BTC", _SIGMA_WINDOW)
    if cached:
        return cached

    values = await _fetch_netflow_history()
    if not values:
        logger.warning("netflow history empty — z-score skip")
        return None

    z = _zscore(values)
    if z is None:
        return None

    payload = {
        "sigma": round(z, 2),
        "current": values[-1],
        "mean_90d": round(statistics.fmean(values[:-1]), 2),
        "stdev_90d": round(statistics.stdev(values[:-1]), 2),
        "samples": len(values),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set("cockpit_netflow_sigma", "BTC", _SIGMA_WINDOW, payload, _SIGMA_TTL)
    return payload


# ──────────────────────────────────────────────────────────────────
# Funding rate z-score
# ──────────────────────────────────────────────────────────────────

async def _fetch_funding_history(days: int = _LOOKBACK_DAYS) -> Optional[list[float]]:
    """Son N gün BTC funding rate değerleri."""
    from_date = (datetime.now(timezone.utc) - timedelta(days=days + 5)).strftime("%Y%m%d")
    raw = await _cq_get(
        "/btc/market-data/funding-rates",
        {"exchange": "all_exchange", "window": "day", "from": from_date, "limit": days + 5},
    )
    if not raw:
        return None
    rows = raw.get("result", {}).get("data", [])
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r.get("date", ""))
    values = [
        float(r["funding_rates"])
        for r in rows
        if r.get("funding_rates") is not None
    ]
    return values[-days:] if len(values) > days else values


async def compute_funding_zscore() -> Optional[dict]:
    """Funding rate için 90g z-score."""
    cached = await _cache_get("cockpit_funding_sigma", "BTC", _SIGMA_WINDOW)
    if cached:
        return cached

    values = await _fetch_funding_history()
    if not values:
        return None

    z = _zscore(values)
    if z is None:
        return None

    payload = {
        "sigma": round(z, 2),
        "current": values[-1],
        "mean_90d": round(statistics.fmean(values[:-1]), 5),
        "stdev_90d": round(statistics.stdev(values[:-1]), 5),
        "samples": len(values),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set("cockpit_funding_sigma", "BTC", _SIGMA_WINDOW, payload, _SIGMA_TTL)
    return payload


# ──────────────────────────────────────────────────────────────────
# BTC price z-score (CoinGecko)
# ──────────────────────────────────────────────────────────────────

async def _fetch_btc_price_history(days: int = _LOOKBACK_DAYS) -> Optional[list[float]]:
    """Son N gün BTC günlük kapanış fiyatı (USD), CoinGecko public API."""
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    params = {"vs_currency": "usd", "days": str(days), "interval": "daily"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params=params)
            if r.status_code != 200:
                logger.warning(f"CoinGecko BTC history status={r.status_code}")
                return None
            data = r.json()
            prices = data.get("prices", [])
            # prices: [[ts_ms, price], ...]
            return [float(p[1]) for p in prices if p and p[1] is not None]
    except Exception as e:
        logger.error(f"CoinGecko BTC history error: {e}")
        return None


async def compute_btc_price_zscore() -> Optional[dict]:
    """BTC günlük fiyat değişim z-score (90g log-return tabanlı)."""
    cached = await _cache_get("cockpit_btcprice_sigma", "BTC", _SIGMA_WINDOW)
    if cached:
        return cached

    prices = await _fetch_btc_price_history()
    if not prices or len(prices) < 31:
        return None

    # Daily log-return serisi (price hareketinin z-score'u, raw fiyatın değil —
    # fiyat seviyesi z-score'u trendde yanıltıcı olur)
    import math
    rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
    z = _zscore(rets)
    if z is None:
        return None

    payload = {
        "sigma": round(z, 2),
        "current_return_pct": round(rets[-1] * 100, 3),
        "mean_90d_pct": round(statistics.fmean(rets[:-1]) * 100, 3),
        "stdev_90d_pct": round(statistics.stdev(rets[:-1]) * 100, 3),
        "samples": len(rets),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set("cockpit_btcprice_sigma", "BTC", _SIGMA_WINDOW, payload, _SIGMA_TTL)
    return payload


# ──────────────────────────────────────────────────────────────────
# Aggregator — tek round-trip için tüm sigma'lar
# ──────────────────────────────────────────────────────────────────

async def get_all_sigmas() -> dict:
    """
    Cockpit chip'leri için tüm σ-sapma değerlerini tek payload'da döner.
    Frontend /api/v1/cockpit/sigmas endpoint'inden bunu çeker.

    Parallel fetch — 3 hesaplama bağımsız, paralel koşar.
    """
    import asyncio

    netflow, funding, btc_price = await asyncio.gather(
        compute_netflow_zscore(),
        compute_funding_zscore(),
        compute_btc_price_zscore(),
        return_exceptions=True,
    )

    def _safe(r):
        return None if isinstance(r, Exception) else r

    return {
        "symbol": "BTC",
        "window_days": _LOOKBACK_DAYS,
        "netflow": _safe(netflow),
        "funding": _safe(funding),
        "btc_price": _safe(btc_price),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
