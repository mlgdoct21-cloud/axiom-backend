"""Akademi 'Gerçek Örnek' canlı veri servisi (Faz 3 köprüsü).

Bir strateji için BUGÜNÜN gerçek rakamlarıyla somut bir örnek üretir. Tüm
sayılar Deribit (kripto opsiyon zinciri) + deterministik payoff/Black-Scholes
matematiğinden gelir — Gemini YOK (eğitimde halüsinasyon ölümcül).

Model: her strateji bir "leg" listesidir (tip=call/put, side=long/short,
moneyness=strike/spot) + opsiyonel underlying pozisyonu (protective put / covered
call gibi spot tutan yapılar). Tek bir generic resolver bacakları gerçek
(Deribit) ya da teorik (Black-Scholes) primlerle çözer; tek bir generic payoff
motoru kâr/zarar eğrisini ve metrikleri üretir. Yeni strateji eklemek =
_STRATEGIES sözlüğüne bir satır.

Veri kaynağı önceliği:
  1) deribit_live          — Deribit public API: gerçek spot + strike + prim + IV.
  2) theoretical_fallback  — Deribit'e ulaşılamazsa: CoinGecko spot + Black-Scholes
     teorik prim (varsayılan IV). UI'da AÇIKÇA 'teorik' etiketlenir.

Fail-soft: hiçbiri olmazsa available=False döner; UI temiz bir mesaj gösterir.
Cache: 60 sn in-process — Deribit/CoinGecko'yu dövme.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from core.logger import get_logger

logger = get_logger("academy_live_example")

_CACHE: dict = {}
_CACHE_TTL = 60
_TIMEOUT = 6
_TARGET_DAYS = 30

# Desteklenen varlıklar — kripto-first (Deribit ücretsiz + tam zincir).
_ASSET_MAP = {
    "BTC": {"index": "btc_usd", "currency": "BTC", "coingecko": "bitcoin", "default_iv": 0.55, "round": -3},
    "ETH": {"index": "eth_usd", "currency": "ETH", "coingecko": "ethereum", "default_iv": 0.65, "round": -2},
}


# ---------------------------------------------------------------------------
# Strateji tanımları (leg-tabanlı)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Leg:
    type: str        # "call" | "put"
    side: str        # "long" | "short"
    moneyness: float  # strike ≈ spot * moneyness


# underlying: 1 birim spot tutuluyorsa +1 (covered call / protective put), yoksa 0.
_STRATEGIES: dict[str, dict] = {
    "protective-put": {
        "underlying": 1.0,
        "legs": [_Leg("put", "long", 0.95)],
    },
    "covered-call": {
        "underlying": 1.0,
        "legs": [_Leg("call", "short", 1.05)],
    },
    "cash-secured-put": {
        "underlying": 0.0,
        "legs": [_Leg("put", "short", 0.95)],
    },
    # Debit spread varsayılan: bull call (yön=yukarı, ucuz tanımlı risk).
    "debit-spread": {
        "underlying": 0.0,
        "legs": [_Leg("call", "long", 1.00), _Leg("call", "short", 1.10)],
    },
    # Credit spread varsayılan: bull put (prim topla, yön=yukarı/yatay).
    "credit-spread": {
        "underlying": 0.0,
        "legs": [_Leg("put", "short", 0.97), _Leg("put", "long", 0.88)],
    },
    # Long straddle: büyük hareket bahsi (yön bağımsız).
    "straddle": {
        "underlying": 0.0,
        "legs": [_Leg("call", "long", 1.00), _Leg("put", "long", 1.00)],
    },
    "iron-condor": {
        "underlying": 0.0,
        "legs": [
            _Leg("put", "long", 0.85),
            _Leg("put", "short", 0.93),
            _Leg("call", "short", 1.07),
            _Leg("call", "long", 1.15),
        ],
    },
}

_SUPPORTED = set(_STRATEGIES)


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


def _bs_call(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """Black-Scholes call fiyatı (USD). Teorik fallback için."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


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


def _deribit_premium(instrument_name: str, spot: float) -> tuple[Optional[float], Optional[float]]:
    tick = _deribit_get("ticker", {"instrument_name": instrument_name})
    if not tick or tick.get("mark_price") is None:
        return None, None
    # Deribit opsiyon primi underlying (BTC/ETH) cinsindendir → USD'ye çevir.
    premium_usd = float(tick["mark_price"]) * spot
    return round(premium_usd, 2), tick.get("mark_iv")


def _assign_strikes(legs: list[_Leg], cands: list[dict], spot: float) -> Optional[dict[int, dict]]:
    """Bir tipteki (call/put) bacaklara DİSTİNCT en yakın gerçek strike ata.

    Greedy: hedef strike sırasına göre, henüz kullanılmamış en yakını seç. Aynı
    strike'a iki bacak çakışırsa (seyrek zincir) yapı bozulurdu — bunu engeller.
    """
    used: set[float] = set()
    out: dict[int, dict] = {}
    for idx, leg in sorted(enumerate(legs), key=lambda t: t[1].moneyness):
        pool = [i for i in cands if i["strike"] not in used]
        if not pool:
            return None
        target_k = spot * leg.moneyness
        inst = min(pool, key=lambda i: abs(i["strike"] - target_k))
        used.add(inst["strike"])
        out[idx] = inst
    return out


def _resolve_live(asset: str, legs: list[_Leg]) -> Optional[dict]:
    """Deribit: gerçek spot, ~30g vade, her bacak için gerçek strike + prim."""
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

    now_ms = time.time() * 1000
    target_exp = now_ms + _TARGET_DAYS * 86400 * 1000
    exps = sorted({i["expiration_timestamp"] for i in ins}, key=lambda e: abs(e - target_exp))
    if not exps:
        return None
    exp = exps[0]
    days = max((exp - now_ms) / 86400000, 0.1)

    by_type: dict[str, list[dict]] = {"call": [], "put": []}
    for i in ins:
        if i["expiration_timestamp"] == exp and i.get("option_type") in by_type:
            by_type[i["option_type"]].append(i)

    # Tipe göre bacakları grupla, distinct strike ata.
    resolved: dict[int, dict] = {}
    for typ in ("call", "put"):
        typ_legs = [(i, leg) for i, leg in enumerate(legs) if leg.type == typ]
        if not typ_legs:
            continue
        if not by_type[typ]:
            return None
        assigned = _assign_strikes([leg for _, leg in typ_legs], by_type[typ], spot)
        if assigned is None:
            return None
        for local_i, (orig_i, leg) in enumerate(typ_legs):
            resolved[orig_i] = assigned[local_i]

    out_legs = []
    iv_acc: list[float] = []
    for i, leg in enumerate(legs):
        inst = resolved[i]
        prem, iv = _deribit_premium(inst["instrument_name"], spot)
        if prem is None:
            return None
        if iv is not None:
            iv_acc.append(float(iv))
        out_legs.append({
            "type": leg.type,
            "side": leg.side,
            "strike": float(inst["strike"]),
            "premium": prem,
            "instrument": inst["instrument_name"],
        })

    return {
        "data_source": "deribit_live",
        "spot": spot,
        "days": round(days, 1),
        "legs": out_legs,
        "iv_pct": round(sum(iv_acc) / len(iv_acc), 1) if iv_acc else None,
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


def _resolve_theoretical(asset: str, legs: list[_Leg]) -> Optional[dict]:
    """Deribit yoksa: gerçek spot (CoinGecko) + Black-Scholes teorik prim."""
    cfg = _ASSET_MAP[asset]
    spot = _fetch_coingecko_spot(asset)
    if not spot:
        return None
    iv = cfg["default_iv"]
    T = _TARGET_DAYS / 365
    out_legs = []
    for leg in legs:
        k = float(round(spot * leg.moneyness, cfg["round"])) or spot * leg.moneyness
        prem = _bs_call(spot, k, T, iv) if leg.type == "call" else _bs_put(spot, k, T, iv)
        out_legs.append({
            "type": leg.type,
            "side": leg.side,
            "strike": k,
            "premium": round(prem, 2),
            "instrument": None,
        })
    return {
        "data_source": "theoretical_fallback",
        "spot": spot,
        "days": float(_TARGET_DAYS),
        "legs": out_legs,
        "iv_pct": round(iv * 100, 1),
    }


# ---------------------------------------------------------------------------
# Payoff motoru (generic)
# ---------------------------------------------------------------------------
def _structure_pnl(st: float, s0: float, underlying: float, legs: list[dict]) -> float:
    pnl = underlying * (st - s0)
    for leg in legs:
        if leg["type"] == "call":
            intrinsic = max(st - leg["strike"], 0.0)
        else:
            intrinsic = max(leg["strike"] - st, 0.0)
        if leg["side"] == "long":
            pnl += intrinsic - leg["premium"]
        else:
            pnl += leg["premium"] - intrinsic
    return pnl


def _breakevens(payoff: list[dict]) -> list[float]:
    """PnL=0 geçişlerini lineer interpolasyonla bul (eğri parçalı-lineer)."""
    out: list[float] = []
    for i in range(1, len(payoff)):
        y0, y1 = payoff[i - 1]["pnl"], payoff[i]["pnl"]
        if (y0 <= 0 <= y1) or (y0 >= 0 >= y1):
            if y1 != y0:
                x0, x1 = payoff[i - 1]["price"], payoff[i]["price"]
                xz = x0 + (x1 - x0) * (0 - y0) / (y1 - y0)
                if not out or abs(xz - out[-1]) > max(1.0, 0.001 * x0):
                    out.append(round(xz, 2))
    return out


def _fmt_usd(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return f"{int(round(n)):,}".replace(",", ".") + " $"


def _build(strategy: str, asset: str) -> dict:
    spec = _STRATEGIES[strategy]
    legs_spec: list[_Leg] = spec["legs"]
    underlying: float = spec["underlying"]

    resolved = _resolve_live(asset, legs_spec) or _resolve_theoretical(asset, legs_spec)
    if not resolved:
        return {"available": False, "asset": asset, "strategy": strategy}

    s0 = resolved["spot"]
    legs = resolved["legs"]
    strikes = [leg["strike"] for leg in legs]

    # net prim: long bacaklar öder (debit), short bacaklar alır (credit).
    net_debit = round(
        sum(leg["premium"] if leg["side"] == "long" else -leg["premium"] for leg in legs),
        2,
    )

    # Görüntü eğrisi: düzgün köşeler için grid ∪ strike-kink noktaları.
    lo = max(0.0, min(0.6 * s0, min(strikes) * 0.85))
    hi = max(1.4 * s0, max(strikes) * 1.15)
    n = 40
    grid = {lo + (hi - lo) * i / n for i in range(n + 1)}
    grid |= {k for k in strikes if lo <= k <= hi}
    grid.add(s0)
    xs = sorted(grid)
    payoff = [{"price": round(x, 2), "pnl": round(_structure_pnl(x, s0, underlying, legs), 2)} for x in xs]

    # Metrik uçları: kink'ler + st=0 (en kötü aşağı) + uzak nokta dahil.
    edge_xs = sorted(set(xs) | {0.0, 3.0 * s0} | set(strikes))
    pnls = [_structure_pnl(x, s0, underlying, legs) for x in edge_xs]
    max_loss = round(min(pnls), 2)

    # Yukarı yönde sınırsız mı? (net long call sayısı + underlying > 0)
    long_calls = sum(1 for leg in legs if leg["type"] == "call" and leg["side"] == "long")
    short_calls = sum(1 for leg in legs if leg["type"] == "call" and leg["side"] == "short")
    up_slope = underlying + long_calls - short_calls
    upside_unbounded = up_slope > 1e-9
    max_profit = None if upside_unbounded else round(max(pnls), 2)

    bes = _breakevens(payoff)

    # ---- metrikler (frontend kind→renk: loss/gain/neutral) ----
    metrics = [
        {"label": "Maks. zarar", "value": max_loss, "kind": "loss"},
        (
            {"label": "Maks. kazanç", "display": "Sınırsız ↑", "value": None, "kind": "gain"}
            if upside_unbounded
            else {"label": "Maks. kazanç", "value": max_profit, "kind": "gain"}
        ),
    ]
    if bes:
        if len(bes) == 1:
            metrics.append({"label": "Başabaş", "value": bes[0], "kind": "neutral"})
        else:
            metrics.append({
                "label": "Başabaş aralığı",
                "display": f"{_fmt_usd(bes[0])} – {_fmt_usd(bes[-1])}",
                "value": None,
                "kind": "neutral",
            })
    if net_debit > 0:
        metrics.append({"label": "Ödenen net prim", "value": net_debit, "kind": "neutral"})
    elif net_debit < 0:
        metrics.append({"label": "Alınan net prim", "value": abs(net_debit), "kind": "gain"})

    summary = _summary(strategy, asset, s0, resolved["days"], legs, net_debit, bes)

    out = {
        "available": True,
        "asset": asset,
        "strategy": strategy,
        "data_source": resolved["data_source"],
        "spot": round(s0, 2),
        "expiry_days": resolved["days"],
        "iv_pct": resolved["iv_pct"],
        "strikes": strikes,
        "strike": strikes[0] if strikes else None,
        "net_premium_usd": net_debit,
        "legs": legs,
        "metrics": metrics,
        "breakevens": bes,
        "summary": summary,
        "payoff": payoff,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # --- protective-put geriye dönük alanlar (eski frontend skew güvenliği) ---
    if strategy == "protective-put" and legs:
        put = legs[0]
        out["premium_usd"] = put["premium"]
        out["instrument"] = put.get("instrument")
        out["max_loss_usd"] = round((s0 - put["strike"]) + put["premium"], 2)
        out["breakeven_up_usd"] = round(s0 + put["premium"], 2)
        out["protected_floor_usd"] = round(put["strike"] - put["premium"], 2)

    return out


# ---------------------------------------------------------------------------
# Senaryo cümlesi (deterministik, strateji-özel)
# ---------------------------------------------------------------------------
def _summary(strategy: str, asset: str, s0: float, days: float, legs: list[dict],
             net_debit: float, bes: list[float]) -> str:
    head = f"Bugün {asset} = {_fmt_usd(s0)}. ~{int(round(days))} günlük vade ile "
    calls = [leg for leg in legs if leg["type"] == "call"]
    puts = [leg for leg in legs if leg["type"] == "put"]

    if strategy == "protective-put":
        p = puts[0]
        return (head + f"{_fmt_usd(p['strike'])} kullanım fiyatlı bir put alırsan "
                f"(prim ≈ {_fmt_usd(p['premium'])}), 1 {asset}'ini aşağı yönlü "
                f"sigortalarsın. Yukarı potansiyelin açık kalır.")

    if strategy == "covered-call":
        c = calls[0]
        return (head + f"1 {asset} tutarken {_fmt_usd(c['strike'])} call satarsan "
                f"prim ≈ {_fmt_usd(c['premium'])} 'kira' toplarsın. Fiyat "
                f"{_fmt_usd(c['strike'])} üstüne çıkarsa kârın orada tavanlanır.")

    if strategy == "cash-secured-put":
        p = puts[0]
        return (head + f"{_fmt_usd(p['strike'])} put satarsan prim ≈ "
                f"{_fmt_usd(p['premium'])} alırsın. Fiyat {_fmt_usd(p['strike'])} "
                f"altına inerse 1 {asset}'i o seviyeden almaya razı olursun "
                f"(indirimli giriş).")

    if strategy == "debit-spread":
        lo_c = min(calls, key=lambda c: c["strike"])
        hi_c = max(calls, key=lambda c: c["strike"])
        return (head + f"{_fmt_usd(lo_c['strike'])} call AL + {_fmt_usd(hi_c['strike'])} "
                f"call SAT → net ödenen prim ≈ {_fmt_usd(net_debit)}. Yukarı yönlü, "
                f"ucuz ve tanımlı risk; kâr {_fmt_usd(hi_c['strike'])} üstünde tavanlanır.")

    if strategy == "credit-spread":
        hi_p = max(puts, key=lambda p: p["strike"])
        lo_p = min(puts, key=lambda p: p["strike"])
        return (head + f"{_fmt_usd(hi_p['strike'])} put SAT + {_fmt_usd(lo_p['strike'])} "
                f"put AL → net alınan prim ≈ {_fmt_usd(abs(net_debit))}. Fiyat "
                f"{_fmt_usd(hi_p['strike'])} üstünde kalırsa primi cebe koyarsın; "
                f"zarar {_fmt_usd(lo_p['strike'])} ile sınırlı.")

    if strategy == "straddle":
        k = calls[0]["strike"]
        return (head + f"{_fmt_usd(k)} call + {_fmt_usd(k)} put AL → net ödenen prim ≈ "
                f"{_fmt_usd(net_debit)}. Büyük hareket beklersin; yön fark etmez, "
                f"yeter ki fiyat başabaşların dışına çıksın.")

    if strategy == "iron-condor":
        be_txt = ""
        if len(bes) >= 2:
            be_txt = f" Fiyat {_fmt_usd(bes[0])}–{_fmt_usd(bes[-1])} aralığında kalırsa primi tutarsın."
        return (head + f"alt put spread + üst call spread satarsın → net alınan prim ≈ "
                f"{_fmt_usd(abs(net_debit))}.{be_txt} İki taraflı, tanımlı riskli kira.")

    return head + "yapıyı kurduğunda vade sonu kâr/zarar profili aşağıdaki gibidir."


# ---------------------------------------------------------------------------
# Genel API
# ---------------------------------------------------------------------------
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

    payload = await asyncio.to_thread(_build, strategy, asset)
    _CACHE[key] = {"payload": payload, "ts": now}
    return payload
