"""Akademi canlı bağlam rozeti (Faz 1.6) — Deribit DVOL (BTC+ETH) + FMP VIX.

Gemini YOK — sadece sayı çek + statik anlatı template'i.
Müfredat (M2L3) IV kavramını öğretirken bu rozet "bugün DVOL X, VIX Y —
M2L3 dersinde gördüğün rejim ölçüsü canlı bu" diyebilsin.

Fail-soft: herhangi bir kaynak başarısızsa kalan değerler yine döner;
hepsi başarısızsa endpoint 200 + boş partial döner (rozet UI'da kaybolur).

Cache: 5dk in-process — bu rozet kritik değil, FMP/Deribit'i dövme.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger("academy_context")

_CACHE: dict = {"payload": None, "ts": 0.0}
_CACHE_TTL = 300  # 5 dk
_TIMEOUT = 6


def _fetch_deribit_dvol(currency: str) -> Optional[float]:
    """Deribit DVOL son saatlik close. Currency: 'BTC' veya 'ETH'."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 6 * 3600 * 1000  # son 6 saat
    try:
        r = requests.get(
            "https://www.deribit.com/api/v2/public/get_volatility_index_data",
            params={
                "currency": currency,
                "start_timestamp": start_ms,
                "end_timestamp": now_ms,
                "resolution": 3600,
            },
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        data = (r.json() or {}).get("result", {}).get("data") or []
        if not data:
            return None
        # Her satır: [ts, open, high, low, close]
        last_close = data[-1][4]
        return float(last_close)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Deribit DVOL fetch failed (%s): %s", currency, exc)
        return None


def _fetch_fmp_vix() -> Optional[float]:
    fmp_key = os.getenv("FMP_API_KEY", "").strip()
    if not fmp_key:
        return None
    try:
        r = requests.get(
            "https://financialmodelingprep.com/stable/quote",
            params={"symbol": "^VIX", "apikey": fmp_key},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        d = r.json()
        q = d[0] if isinstance(d, list) and d else None
        if not q or q.get("price") is None:
            return None
        return float(q["price"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("FMP VIX fetch failed: %s", exc)
        return None


def _vix_regime(vix: Optional[float]) -> Optional[str]:
    if vix is None:
        return None
    if vix < 14:
        return "Sakin — opsiyonlar görece ucuz; sigorta pencere fırsatı."
    if vix < 20:
        return "Normal — IV rejimi tipik."
    if vix < 30:
        return "Endişeli — primler şişti, sigorta pahalı."
    return "Panik fiyatlaması — yangın başladıktan sonra sigorta mantıksız."


def _dvol_regime(dvol: Optional[float], asset: str) -> Optional[str]:
    """BTC tipik aralık ~40-80, ETH ~60-100 (kabaca). Rejim kategorize et."""
    if dvol is None:
        return None
    if asset == "BTC":
        low, mid = 45, 65
    else:  # ETH
        low, mid = 55, 80
    if dvol < low:
        return f"{asset} DVOL düşük — opsiyon almak ucuz, satmak az kazandırır."
    if dvol < mid:
        return f"{asset} DVOL ortalama rejim."
    return f"{asset} DVOL yüksek — opsiyon satışı (covered call, CSP) prim açısından cazip."


async def get_live_context(force: bool = False) -> dict:
    """Tek payload — BTC/ETH DVOL + VIX + her biri için kısa statik anlatı.

    Dönüş şeması (kısmi olabilir):
    {
      "btc_dvol": 52.3, "btc_note": "...",
      "eth_dvol": 71.0, "eth_note": "...",
      "vix": 16.8, "vix_note": "...",
      "generated_at": "ISO-8601",
      "stale": false
    }
    """
    now = time.time()
    if not force and _CACHE["payload"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        return {**_CACHE["payload"], "stale": False}

    # Paralel fetch — to_thread ile blocking requests sarımı.
    btc, eth, vix = await asyncio.gather(
        asyncio.to_thread(_fetch_deribit_dvol, "BTC"),
        asyncio.to_thread(_fetch_deribit_dvol, "ETH"),
        asyncio.to_thread(_fetch_fmp_vix),
    )

    from datetime import datetime, timezone

    payload = {
        "btc_dvol": round(btc, 2) if btc is not None else None,
        "btc_note": _dvol_regime(btc, "BTC"),
        "eth_dvol": round(eth, 2) if eth is not None else None,
        "eth_note": _dvol_regime(eth, "ETH"),
        "vix": round(vix, 2) if vix is not None else None,
        "vix_note": _vix_regime(vix),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Hepsi None ise eski cache varsa onu döndür ('stale' işaretle).
    if all(payload[k] is None for k in ("btc_dvol", "eth_dvol", "vix")):
        if _CACHE["payload"]:
            return {**_CACHE["payload"], "stale": True}
        return {**payload, "stale": True}

    _CACHE["payload"] = payload
    _CACHE["ts"] = now
    return {**payload, "stale": False}
