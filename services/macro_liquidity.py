"""
Macro Liquidity Service — Global Likidite Endeksi.

Dashboard'daki Makro Pulse chip'inin tertiary satırında gösterilen "merkez
bankası likidite" verisi. Fed M2 + ECB Total Assets, USD-converted.

Veri kaynakları (FRED):
  - WM2NS: Weekly M2 Money Stock NSA (US, milyar USD)
  - ECBASSETS: ECB Total Assets (milyon EUR)
  - DEXUSEU: EUR/USD daily exchange rate

Cache: cryptoquant_cache postgres tablosu, window='liquidity_v1', 6 saatlik TTL.
Bu veriler haftalık güncellenir, daha sık fetch gereksiz.
"""

from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from services.cryptoquant_service import _cache_get, _cache_set
from services.macro_sources.fred_api import fetch_fred_series

logger = logging.getLogger("macro_liquidity")

_WINDOW = "liquidity_v1"
_TTL = timedelta(hours=6)


async def _fetch_fred_history(series_id: str, limit: int = 90) -> Optional[list[float]]:
    """FRED'den son N observation'ın value listesi (kronolojik, eski→yeni)."""
    result = await fetch_fred_series(series_id, limit=limit)
    if result.error or not result.observations:
        logger.warning(f"FRED {series_id} error or empty: {result.error}")
        return None
    obs = list(reversed(result.observations))  # FRED yeni→eski, biz tersine al
    values = []
    for o in obs:
        try:
            v = float(o.get("value", "."))
            if not math.isnan(v):
                values.append(v)
        except (ValueError, TypeError):
            continue
    return values if values else None


async def compute_global_liquidity() -> Optional[dict]:
    """
    Global likidite endeksi: Fed M2 + ECB Total Assets (USD'ye çevrili).
    Son ~13 haftalık seri, 30 gün (4 hafta) değişim yüzdesi, trend etiketi.
    """
    cached = await _cache_get("macro_global_liquidity", "GLOBAL", _WINDOW)
    if cached:
        return cached

    # M2 (USD billions, weekly): WM2NS
    m2 = await _fetch_fred_history("WM2NS", limit=15)
    # ECB Total Assets (million EUR, weekly): ECBASSETS
    ecb = await _fetch_fred_history("ECBASSETS", limit=15)
    # EUR/USD daily (DEXUSEU)
    eurusd = await _fetch_fred_history("DEXUSEU", limit=90)

    if not m2 or not ecb:
        logger.warning("liquidity: M2 or ECB unavailable")
        return None

    eurusd_avg = statistics.fmean(eurusd[-30:]) if eurusd else 1.07

    # USD-converted toplam seri (milyar USD)
    total_series = []
    min_len = min(len(m2), len(ecb))
    for i in range(min_len):
        m2_bn = m2[-(min_len - i)]               # billions USD
        ecb_bn_usd = (ecb[-(min_len - i)] / 1000.0) * eurusd_avg  # mn EUR → bn USD
        total_series.append(m2_bn + ecb_bn_usd)

    if len(total_series) < 5:
        return None

    current_trn = total_series[-1] / 1000.0  # billion → trillion
    if len(total_series) >= 4:
        ch_30d_pct = (total_series[-1] - total_series[-4]) / total_series[-4] * 100.0
    else:
        ch_30d_pct = 0.0

    trend = "genişleme" if ch_30d_pct > 0.5 else "sıkışma" if ch_30d_pct < -0.5 else "yatay"

    payload = {
        "value_trn": round(current_trn, 2),
        "ch_30d_pct": round(ch_30d_pct, 2),
        "trend": trend,
        "components": {
            "fed_m2_bn": round(m2[-1], 1),
            "ecb_total_bn_usd": round((ecb[-1] / 1000.0) * eurusd_avg, 1),
        },
        "eurusd_avg": round(eurusd_avg, 4),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set("macro_global_liquidity", "GLOBAL", _WINDOW, payload, _TTL)
    return payload
