"""Akademi 'Gerçek Örnek' canlı veri servisi (Faz 3 köprüsü).

Bir strateji için BUGÜNÜN gerçek rakamlarıyla somut bir örnek üretir. Tüm
sayılar Deribit (kripto opsiyon zinciri) + deterministik payoff/Black-Scholes
matematiğinden gelir — Gemini YOK (eğitimde halüsinasyon ölümcül).

Veri kaynağı önceliği:
  1) deribit_live          — Deribit public API: gerçek spot + strike + prim + IV.
  2) theoretical_fallback  — Deribit'e ulaşılamazsa: CoinGecko spot + Black-Scholes
     teorik prim (varsayılan IV). UI'da AÇIKÇA 'teorik' etiketlenir; prod'da (1)
     devreye girdiğinde gerçek piyasa primi gösterilir.

Fail-soft: hiçbiri olmazsa available=False döner; UI temiz bir mesaj gösterir.
Cache: 60 sn in-process — Deribit/CoinGecko'yu dövme.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger("academy_live_example")

_CACHE: dict = {}
_CACHE_TTL = 60
_TIMEOUT = 6

# Desteklenen varlıklar — kripto-first (Deribit ücretsiz + tam zincir).
_ASSET_MAP = {
    "BTC": {"index": "btc_usd", "currency": "BTC", "coingecko": "bitcoin", "default_iv": 0.55, "round": -3},
    "ETH": {"index": "eth_usd", "currency": "ETH", "coingecko": "ethereum", "default_iv": 0.65, "round": -2},
}

# Faz: şimdilik sadece protective-put prototipi.
_SUPPORTED = {"protective-put"}


# ---------------------------------------------------------------------------
# Matematik (deterministik — kod tarafında)
# ---------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_put(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """Black-Scholes put fiyatı (USD). Teorik fallback için."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


# ---------------------------------------------------------------------------
# Veri kaynakları
# ---------------------------------------------------------------------------
def _deribit_get(path: str, params: dict) -> Optional[dict]:
    try:
        r = requests.get(
            f"https://www.deribit.com/api/v2/public/{path}",
            params=params,
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("result")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Deribit %s failed: %s", path, exc)
        return None


def _fetch_deribit_protective_put(asset: str) -> Optional[dict]:
    """Gerçek Deribit zincirinden ~30 günlük, ~%5 OTM put seç."""
    cfg = _ASSET_MAP[asset]
    idx = _deribit_get("get_index_price", {"index_name": cfg["index"]})
    if not idx or not idx.get("index_price"):
        return None
    spot = float(idx["index_price"])

    ins = _deribit_get(
        "get_instruments",
        {"currency": cfg["currency"], "kind": "option", "expired": "false"},
    )
    if not ins:
        return None
    puts = [i for i in ins if i.get("option_type") == "put"]
    if not puts:
        return None

    now_ms = time.time() * 1000
    target_exp = now_ms + 30 * 86400 * 1000
    exps = sorted({i["expiration_timestamp"] for i in puts}, key=lambda e: abs(e - target_exp))
    exp = exps[0]
    days = max((exp - now_ms) / 86400000, 0.1)

    target_strike = spot * 0.95
    cand = [i for i in puts if i["expiration_timestamp"] == exp]
    best = min(cand, key=lambda i: abs(i["strike"] - target_strike))

    tick = _deribit_get("ticker", {"instrument_name": best["instrument_name"]})
    if not tick or tick.get("mark_price") is None:
        return None
    # Deribit opsiyon primi underlying (BTC/ETH) cinsindendir → USD'ye çevir.
    mark_underlying = float(tick["mark_price"])
    premium_usd = mark_underlying * spot
    iv = tick.get("mark_iv")

    return {
        "data_source": "deribit_live",
        "spot": spot,
        "strike": float(best["strike"]),
        "expiry_days": round(days, 1),
        "premium_usd": round(premium_usd, 2),
        "iv_pct": round(float(iv), 1) if iv is not None else None,
        "instrument": best["instrument_name"],
    }


def _fetch_coingecko_spot(asset: str) -> Optional[float]:
    cfg = _ASSET_MAP[asset]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cfg["coingecko"], "vs_currencies": "usd"},
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        return float(r.json()[cfg["coingecko"]]["usd"])
    except Exception as exc:  # noqa: BLE001
        logger.debug("CoinGecko spot failed: %s", exc)
        return None


def _theoretical_protective_put(asset: str) -> Optional[dict]:
    """Deribit yoksa: gerçek spot (CoinGecko) + Black-Scholes teorik prim."""
    cfg = _ASSET_MAP[asset]
    spot = _fetch_coingecko_spot(asset)
    if not spot:
        return None
    iv = cfg["default_iv"]
    K = float(round(spot * 0.95, cfg["round"])) or spot * 0.95
    T = 30 / 365
    premium = _bs_put(spot, K, T, iv)
    return {
        "data_source": "theoretical_fallback",
        "spot": spot,
        "strike": K,
        "expiry_days": 30.0,
        "premium_usd": round(premium, 2),
        "iv_pct": round(iv * 100, 1),
        "instrument": None,
    }


# ---------------------------------------------------------------------------
# Strateji kurucu
# ---------------------------------------------------------------------------
def _build_protective_put(asset: str) -> dict:
    leg = _fetch_deribit_protective_put(asset) or _theoretical_protective_put(asset)
    if not leg:
        return {"available": False, "asset": asset, "strategy": "protective-put"}

    s0 = leg["spot"]
    k = leg["strike"]
    p = leg["premium_usd"]

    max_loss = round((s0 - k) + p, 2)        # en kötü USD zarar (1 birim varlık)
    breakeven_up = round(s0 + p, 2)          # yukarı başabaş
    protected_floor = round(k - p, 2)        # net taban değer

    payoff = []
    lo, hi = 0.6 * s0, 1.4 * s0
    n = 24
    for i in range(n + 1):
        st = lo + (hi - lo) * i / n
        pnl = max(st, k) - s0 - p
        payoff.append({"price": round(st, 2), "pnl": round(pnl, 2)})

    return {
        "available": True,
        "asset": asset,
        "strategy": "protective-put",
        "data_source": leg["data_source"],
        "spot": round(s0, 2),
        "strike": k,
        "expiry_days": leg["expiry_days"],
        "premium_usd": p,
        "iv_pct": leg["iv_pct"],
        "instrument": leg.get("instrument"),
        "max_loss_usd": max_loss,
        "breakeven_up_usd": breakeven_up,
        "protected_floor_usd": protected_floor,
        "payoff": payoff,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


_BUILDERS = {
    "protective-put": _build_protective_put,
}


async def get_live_example(strategy: str, asset: str = "BTC", force: bool = False) -> dict:
    strategy = (strategy or "").lower().strip()
    asset = (asset or "BTC").upper().strip()
    if strategy not in _SUPPORTED:
        return {"available": False, "error": "unsupported_strategy", "strategy": strategy}
    if asset not in _ASSET_MAP:
        return {"available": False, "error": "unsupported_asset", "asset": asset}

    key = f"{strategy}:{asset}"
    now = time.time()
    if not force and key in _CACHE and (now - _CACHE[key]["ts"]) < _CACHE_TTL:
        return _CACHE[key]["payload"]

    payload = await asyncio.to_thread(_BUILDERS[strategy], asset)
    _CACHE[key] = {"payload": payload, "ts": now}
    return payload
