"""
Cockpit CVD Service — BTC Cumulative Volume Delta (24h).

OnChain panel'in "Türev Piyasası" bölümünde gösterilen CVD verisi.
CryptoQuant snapshot'ında CVD yok — Binance public klines'tan kendimiz
hesaplıyoruz.

Hesap mantığı (Binance USDT-M futures klines, 1h × 24 bar):
  - Her kline'ın takerBuyQuoteVolume → agresif alıcı USD
  - quoteVolume - takerBuyQuoteVolume → agresif satıcı USD
  - delta = toplam buy - toplam sell
  - Fiyat ile uyum: delta yönü ve 24h fiyat değişimi aynı yönde mi?

Cache: cryptoquant_cache postgres tablosu, window='cvd_v1', 15 dakika TTL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from services.cryptoquant_service import _cache_get, _cache_set

logger = logging.getLogger("cockpit_cvd")

_WINDOW = "cvd_v1"
_TTL = timedelta(minutes=15)


async def compute_cvd() -> Optional[dict]:
    """
    CVD 24h: Binance klines 1h × 24 bar, takerBuyQuoteVolume delta.

    Returns:
      {
        "delta_24h_usd": net buy - net sell,
        "total_buy_usd": ...,
        "total_sell_usd": ...,
        "price_change_pct_24h": ...,
        "divergence": "uyumlu" | "uyumsuz",
        "meaning": narrative TR,
        "computed_at": ISO,
      }
    """
    cached = await _cache_get("cvd_btc", "BTC", _WINDOW)
    if cached:
        return cached

    url = "https://fapi.binance.com/fapi/v1/klines"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, params={
                "symbol": "BTCUSDT",
                "interval": "1h",
                "limit": 24,
            })
            if r.status_code != 200:
                logger.warning(f"Binance klines status={r.status_code}")
                return None
            rows = r.json()
            if not rows:
                return None

            total_buy_usd = 0.0
            total_sell_usd = 0.0
            for row in rows:
                try:
                    quote_vol = float(row[7])
                    taker_buy_quote = float(row[10])
                    taker_sell_quote = quote_vol - taker_buy_quote
                    total_buy_usd += taker_buy_quote
                    total_sell_usd += taker_sell_quote
                except (ValueError, IndexError):
                    continue

            delta = total_buy_usd - total_sell_usd
            last_close = float(rows[-1][4])
            first_open = float(rows[0][1])
            price_change_pct = (last_close - first_open) / first_open * 100 if first_open else 0

            divergence: str = "uyumlu"
            if (delta > 0 and price_change_pct < -0.5) or (delta < 0 and price_change_pct > 0.5):
                divergence = "uyumsuz"

            if divergence == "uyumlu":
                if delta > 0:
                    meaning = "Alıcı baskı fiyatla aynı yönde — sağlıklı yükseliş"
                else:
                    meaning = "Satıcı baskı fiyatla aynı yönde — düzeltme sağlıklı"
            else:
                meaning = "CVD ile fiyat ayrışıyor — trend zayıf, dönüş yakın olabilir"

            payload = {
                "delta_24h_usd": round(delta, 0),
                "total_buy_usd": round(total_buy_usd, 0),
                "total_sell_usd": round(total_sell_usd, 0),
                "price_change_pct_24h": round(price_change_pct, 2),
                "divergence": divergence,
                "meaning": meaning,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }
            await _cache_set("cvd_btc", "BTC", _WINDOW, payload, _TTL)
            return payload
    except Exception as e:
        logger.error(f"CVD compute error: {e}")
        return None
