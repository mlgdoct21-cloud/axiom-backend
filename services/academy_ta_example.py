"""AXIOM Teknik Analiz Akademisi — 'Gerçek Örnek' canlı motoru.

Bir TA tekniği için BUGÜNÜN gerçek OHLC verisiyle somut bir örnek üretir.
Tüm sayılar Binance (kripto) veya FMP (US hisse) gerçek pazardan gelir;
indikator + formasyon tespiti deterministik (services/ta_indicators.py).
Gemini YOK (eğitimde halüsinasyon ölümcül).

6 teknik (Faz 1):
  1. engulfing-reversal     — Boğa/Ayı yutan formasyonu (FREE TADIMLIK)
  2. support-resistance-bounce — Destek/dirençten dönüş
  3. trendline-break        — Trend çizgisi kırılımı
  4. head-shoulders         — Omuz-Baş-Omuz formasyonu (& ters)
  5. double-bottom          — İkili dip
  6. triangle-breakout      — Üçgen kırılımı

Veri kaynağı önceliği:
  - kind=crypto  → Binance public klines (BTCUSDT, ETHUSDT; ücretsiz, no auth)
  - kind=equity  → FMP /historical-price-full (FMP_API_KEY env)

Cache: 5 dk in-process. Fail-soft: kaynak hata verirse available=False.
"""
from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from core.logger import get_logger
from services import ta_indicators as ti

logger = get_logger("academy_ta_example")

_CACHE: dict = {}
_CACHE_TTL = 300  # 5 dk
_TIMEOUT = 8
_DEFAULT_BARS = 200

# ---------------------------------------------------------------------------
# Desteklenen varlıklar
# ---------------------------------------------------------------------------
_ASSET_MAP = {
    "BTC":  {"kind": "crypto", "binance": "BTCUSDT", "currency": "USD", "label": "BTC",  "round": 0},
    "ETH":  {"kind": "crypto", "binance": "ETHUSDT", "currency": "USD", "label": "ETH",  "round": 1},
    "SPY":  {"kind": "equity", "fmp": "SPY",  "currency": "USD", "label": "SPY",  "round": 2},
    "QQQ":  {"kind": "equity", "fmp": "QQQ",  "currency": "USD", "label": "QQQ",  "round": 2},
    "AAPL": {"kind": "equity", "fmp": "AAPL", "currency": "USD", "label": "AAPL", "round": 2},
    "NVDA": {"kind": "equity", "fmp": "NVDA", "currency": "USD", "label": "NVDA", "round": 2},
}


# ---------------------------------------------------------------------------
# Veri kaynakları
# ---------------------------------------------------------------------------
def _fetch_binance(symbol: str, interval: str = "1d", limit: int = _DEFAULT_BARS) -> list[dict]:
    """Binance public klines. Auth gerekmez."""
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    bars = []
    for row in data:
        bars.append({
            "t": int(row[0]),  # epoch ms
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[5]),
        })
    return bars


def _fetch_fmp_history(symbol: str, days: int = _DEFAULT_BARS) -> list[dict]:
    """FMP /historical-price-full. Günlük OHLC."""
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FMP_API_KEY yok")
    url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{symbol}"
    r = requests.get(url, params={"apikey": api_key, "timeseries": days}, timeout=_TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    raw = payload.get("historical", []) if isinstance(payload, dict) else []
    # FMP yeni→eski döner; biz eski→yeni istiyoruz
    raw.reverse()
    bars = []
    for row in raw:
        try:
            t_str = row.get("date")  # "YYYY-MM-DD"
            dt = datetime.strptime(t_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            bars.append({
                "t": int(dt.timestamp() * 1000),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row.get("volume") or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return bars


def _fetch_bars(asset: str) -> tuple[list[dict], str]:
    """(bars, source) döndürür. Cache var; cache hit'te source='cache'."""
    cfg = _ASSET_MAP.get(asset)
    if not cfg:
        raise ValueError(f"Desteklenmeyen asset: {asset}")

    key = f"bars:{asset}"
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached["t"] < _CACHE_TTL:
        return cached["bars"], cached["source"]

    if cfg["kind"] == "crypto":
        bars = _fetch_binance(cfg["binance"], "1d", _DEFAULT_BARS)
        source = "binance"
    else:
        bars = _fetch_fmp_history(cfg["fmp"], _DEFAULT_BARS)
        source = "fmp"

    _CACHE[key] = {"t": now, "bars": bars, "source": source}
    return bars, source


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------
def _fmt(price: float, decimals: int) -> str:
    if decimals <= 0:
        return f"{round(price):,}"
    return f"{price:,.{decimals}f}"


def _last_n_bars(bars: list[dict], n: int) -> list[dict]:
    return bars[-n:] if len(bars) > n else bars


def _trend_label(bars: list[dict]) -> str:
    """Son 50 barın EMA20 eğimine bakarak basit trend etiketi."""
    closes = ti.closes(bars[-60:])
    if len(closes) < 30:
        return "Yetersiz veri"
    ema20 = ti.ema(closes, 20)
    valid = [v for v in ema20 if v is not None]
    if len(valid) < 5:
        return "Yetersiz veri"
    recent = valid[-5:]
    older = valid[-20:-5] if len(valid) >= 20 else valid[:-5]
    if not older:
        return "Belirsiz"
    slope = (recent[-1] - older[0]) / max(abs(older[0]), 1e-9)
    if slope > 0.03:
        return "Yükseliş"
    if slope < -0.03:
        return "Düşüş"
    return "Yatay"


# ---------------------------------------------------------------------------
# DETECT — Teknik #1: Yutan Formasyon (engulfing-reversal)
# ---------------------------------------------------------------------------
def _detect_engulfing(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    rsi14 = ti.rsi(closes, 14)
    ema20 = ti.ema(closes, 20)

    # Son 30 mumda yutan formasyon ara (en yenisini al)
    n = len(bars)
    found = None
    for i in range(n - 1, max(n - 30, 1), -1):
        prev, curr = bars[i - 1], bars[i]
        if ti.is_bullish_engulfing(prev, curr):
            found = {"i": i, "side": "bullish", "label": "Boğa Yutan"}
            break
        if ti.is_bearish_engulfing(prev, curr):
            found = {"i": i, "side": "bearish", "label": "Ayı Yutan"}
            break

    decimals = cfg["round"]
    current = bars[-1]["c"]
    annotations = []
    metrics = []
    trend = _trend_label(bars)

    if found:
        bar = bars[found["i"]]
        prev_bar = bars[found["i"] - 1]
        # Hacim teyidi (eğer hacim varsa)
        recent_vols = [b["v"] for b in bars[max(0, found["i"] - 20):found["i"]] if b.get("v", 0) > 0]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        cur_vol = bar.get("v", 0)
        vol_x = (cur_vol / avg_vol) if avg_vol > 0 else 0

        bars_ago = n - 1 - found["i"]
        annotations.append({
            "type": "pattern",
            "i": found["i"],
            "side": found["side"],
            "label": found["label"],
            "price_open": bar["o"],
            "price_close": bar["c"],
        })
        rsi_val = rsi14[found["i"]] if found["i"] < len(rsi14) else None
        metrics = [
            {"label": "Tespit", "value": f"{found['label']} ({bars_ago} bar önce)"},
            {"label": "Yutulan gövde", "value": f"{_fmt(prev_bar['o'], decimals)} → {_fmt(prev_bar['c'], decimals)}"},
            {"label": "Yutan gövde", "value": f"{_fmt(bar['o'], decimals)} → {_fmt(bar['c'], decimals)}"},
            {"label": "Hacim teyidi", "value": f"{vol_x:.1f}x ortalama" + (" (güçlü)" if vol_x >= 1.5 else " (zayıf)") if avg_vol > 0 else "—"},
            {"label": "RSI(14)", "value": f"{rsi_val:.1f}" if rsi_val else "—"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": trend},
        ]
        headline = f"{cfg['label']} günlükte {found['label']} tespit edildi ({bars_ago} bar önce)."
    else:
        metrics = [
            {"label": "Tespit", "value": "Son 30 bar içinde yutan formasyon yok"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "RSI(14)", "value": f"{rsi14[-1]:.1f}" if rsi14[-1] else "—"},
            {"label": "Trend", "value": trend},
        ]
        headline = f"{cfg['label']}'da şu an yutan formasyon yok — sözlüksel örnek."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ema20, "rsi14": rsi14},
        "found": bool(found),
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #2: Destek/Direnç Sıçraması
# ---------------------------------------------------------------------------
def _detect_sr_bounce(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    swings = ti.find_swings(bars, left=5, right=5)
    current = closes[-1]
    decimals = cfg["round"]

    # Yakın geçmiş swing seviyelerini al, fiyata yakın olanları S/R adayı say
    recent_swings = [s for s in swings["highs"] + swings["lows"] if s["i"] >= len(bars) - 120]
    recent_swings.sort(key=lambda s: s["price"])

    # Spot'a en yakın destek (altta) ve direnç (üstte)
    supports = [s for s in recent_swings if s["price"] < current]
    resistances = [s for s in recent_swings if s["price"] > current]
    nearest_sup = max(supports, key=lambda s: s["price"]) if supports else None
    nearest_res = min(resistances, key=lambda s: s["price"]) if resistances else None

    annotations = []
    if nearest_sup:
        annotations.append({
            "type": "level",
            "price": nearest_sup["price"],
            "label": "Destek",
            "side": "support",
        })
    if nearest_res:
        annotations.append({
            "type": "level",
            "price": nearest_res["price"],
            "label": "Direnç",
            "side": "resistance",
        })

    # Pivot noktalarını işaretle (görsel için)
    for sw in swings["highs"][-6:]:
        annotations.append({"type": "pivot", "i": sw["i"], "price": sw["price"], "side": "high"})
    for sw in swings["lows"][-6:]:
        annotations.append({"type": "pivot", "i": sw["i"], "price": sw["price"], "side": "low"})

    sup_dist = ((current - nearest_sup["price"]) / current * 100) if nearest_sup else None
    res_dist = ((nearest_res["price"] - current) / current * 100) if nearest_res else None

    metrics = [
        {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
        {"label": "En yakın destek", "value": _fmt(nearest_sup["price"], decimals) if nearest_sup else "—"},
        {"label": "Destek mesafesi", "value": f"-%{sup_dist:.1f}" if sup_dist is not None else "—"},
        {"label": "En yakın direnç", "value": _fmt(nearest_res["price"], decimals) if nearest_res else "—"},
        {"label": "Direnç mesafesi", "value": f"+%{res_dist:.1f}" if res_dist is not None else "—"},
        {"label": "Trend", "value": _trend_label(bars)},
    ]

    if nearest_sup and sup_dist is not None and sup_dist < 3.0:
        headline = f"{cfg['label']} desteğe yakın — {_fmt(nearest_sup['price'], decimals)} sığlığında."
    elif nearest_res and res_dist is not None and res_dist < 3.0:
        headline = f"{cfg['label']} dirence yakın — {_fmt(nearest_res['price'], decimals)} sınamasında."
    else:
        headline = f"{cfg['label']} S/R bandı arasında: {_fmt(nearest_sup['price'] if nearest_sup else 0, decimals)} ↔ {_fmt(nearest_res['price'] if nearest_res else 0, decimals)}."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20)},
        "found": bool(nearest_sup or nearest_res),
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #3: Trend Çizgisi Kırılımı
# ---------------------------------------------------------------------------
def _detect_trendline(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    swings = ti.find_swings(bars, left=5, right=5)
    current = closes[-1]
    decimals = cfg["round"]

    # Trend yönüne göre swing setini seç
    trend = _trend_label(bars)
    if trend == "Düşüş":
        # düşüş trend çizgisi LH'leri bağlar
        pts = [(s["i"], s["price"]) for s in swings["highs"] if s["i"] >= len(bars) - 100]
        line_label = "Düşüş Trend Çizgisi"
    else:
        # yükseliş ve yatayda HL bağlanır
        pts = [(s["i"], s["price"]) for s in swings["lows"] if s["i"] >= len(bars) - 100]
        line_label = "Yükseliş Trend Çizgisi"

    annotations = []
    line_data = None
    if len(pts) >= 2:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        lr = ti.linear_regression(xs, ys)
        # Çizginin son bardaki değeri
        line_at_now = lr["slope"] * (len(bars) - 1) + lr["intercept"]
        # Çizgi başlangıcı: ilk swing barı; bitiş: son bar
        start_i = xs[0]
        end_i = len(bars) - 1
        line_data = {
            "type": "trendline",
            "from_i": start_i,
            "from_price": lr["slope"] * start_i + lr["intercept"],
            "to_i": end_i,
            "to_price": line_at_now,
            "label": line_label,
            "r2": lr["r2"],
        }
        annotations.append(line_data)
        # Pivotları da işaretle
        for i, p in pts[-5:]:
            annotations.append({"type": "pivot", "i": i, "price": p, "side": "high" if trend == "Düşüş" else "low"})

        dist_pct = (current - line_at_now) / current * 100
        metrics = [
            {"label": "Trend", "value": trend},
            {"label": "Çizgi noktası (şimdi)", "value": _fmt(line_at_now, decimals)},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Çizgiden mesafe", "value": f"{dist_pct:+.1f}%"},
            {"label": "Çizgi sağlamlığı (R²)", "value": f"{lr['r2']:.2f}"},
            {"label": "Swing nokta sayısı", "value": str(len(pts))},
        ]

        if trend == "Düşüş" and current > line_at_now:
            headline = f"{cfg['label']} düşüş çizgisinin üstüne çıktı — kırılım adayı."
        elif trend != "Düşüş" and current < line_at_now:
            headline = f"{cfg['label']} yükseliş çizgisinin altına indi — kırılım adayı."
        else:
            headline = f"{cfg['label']} {line_label.lower()} içinde — yapısı sağlam."
    else:
        metrics = [
            {"label": "Trend", "value": trend},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Durum", "value": "Yeterli swing noktası yok"},
        ]
        headline = f"{cfg['label']} için yeterli swing noktası yok."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20)},
        "found": line_data is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #4: Omuz-Baş-Omuz
# ---------------------------------------------------------------------------
def _detect_hs(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    decimals = cfg["round"]
    swings = ti.find_swings(bars, left=6, right=6)
    highs = sorted(swings["highs"], key=lambda s: s["i"])
    lows = sorted(swings["lows"], key=lambda s: s["i"])
    current = closes[-1]

    # H&S için son 3 zirve: orta en yüksek, iki yan benzer
    pattern = None
    if len(highs) >= 3:
        h1, h2, h3 = highs[-3], highs[-2], highs[-1]
        is_head_middle = h2["price"] > h1["price"] and h2["price"] > h3["price"]
        shoulders_similar = abs(h1["price"] - h3["price"]) / max(h2["price"], 1) < 0.06
        if is_head_middle and shoulders_similar:
            # Boyun çizgisi: ortadaki iki dip
            mid_lows = [l for l in lows if h1["i"] < l["i"] < h3["i"]]
            if len(mid_lows) >= 2:
                neckline = (mid_lows[0]["price"] + mid_lows[-1]["price"]) / 2
                pattern = {
                    "kind": "H&S",
                    "left_shoulder": h1,
                    "head": h2,
                    "right_shoulder": h3,
                    "neckline": neckline,
                    "target": neckline - (h2["price"] - neckline),
                }

    # Ters H&S için son 3 dip
    if not pattern and len(lows) >= 3:
        l1, l2, l3 = lows[-3], lows[-2], lows[-1]
        is_head_middle = l2["price"] < l1["price"] and l2["price"] < l3["price"]
        shoulders_similar = abs(l1["price"] - l3["price"]) / max(l1["price"], 1) < 0.06
        if is_head_middle and shoulders_similar:
            mid_highs = [h for h in highs if l1["i"] < h["i"] < l3["i"]]
            if len(mid_highs) >= 2:
                neckline = (mid_highs[0]["price"] + mid_highs[-1]["price"]) / 2
                pattern = {
                    "kind": "Ters H&S",
                    "left_shoulder": l1,
                    "head": l2,
                    "right_shoulder": l3,
                    "neckline": neckline,
                    "target": neckline + (neckline - l2["price"]),
                }

    annotations = []
    metrics = []
    if pattern:
        annotations.append({"type": "pivot", "i": pattern["left_shoulder"]["i"], "price": pattern["left_shoulder"]["price"], "side": "high" if pattern["kind"] == "H&S" else "low", "label": "Sol Omuz"})
        annotations.append({"type": "pivot", "i": pattern["head"]["i"], "price": pattern["head"]["price"], "side": "high" if pattern["kind"] == "H&S" else "low", "label": "Baş"})
        annotations.append({"type": "pivot", "i": pattern["right_shoulder"]["i"], "price": pattern["right_shoulder"]["price"], "side": "high" if pattern["kind"] == "H&S" else "low", "label": "Sağ Omuz"})
        annotations.append({
            "type": "neckline",
            "from_i": pattern["left_shoulder"]["i"],
            "to_i": pattern["right_shoulder"]["i"],
            "price": pattern["neckline"],
            "label": "Boyun Çizgisi",
        })
        metrics = [
            {"label": "Formasyon", "value": pattern["kind"]},
            {"label": "Baş seviyesi", "value": _fmt(pattern["head"]["price"], decimals)},
            {"label": "Boyun çizgisi", "value": _fmt(pattern["neckline"], decimals)},
            {"label": "Hedef projeksiyonu", "value": _fmt(pattern["target"], decimals)},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Durum", "value": "Boyun çizgisi henüz kırılmadı — formasyon onayı bekliyor" if (pattern["kind"] == "H&S" and current > pattern["neckline"]) or (pattern["kind"] == "Ters H&S" and current < pattern["neckline"]) else "Boyun çizgisi kırıldı — formasyon aktif"},
        ]
        headline = f"{cfg['label']} grafiğinde potansiyel {pattern['kind']} tespit edildi."
    else:
        metrics = [
            {"label": "Tespit", "value": "Son 200 bar içinde H&S yapısı bulunamadı"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        headline = f"{cfg['label']} için H&S yapısı şu an aktif değil — sözlüksel örnek."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20)},
        "found": pattern is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #5: İkili Dip
# ---------------------------------------------------------------------------
def _detect_double_bottom(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    swings = ti.find_swings(bars, left=5, right=5)
    lows = sorted(swings["lows"], key=lambda s: s["i"])
    highs = sorted(swings["highs"], key=lambda s: s["i"])
    current = closes[-1]
    decimals = cfg["round"]

    pattern = None
    # Son 2 dibi al, benzer seviyede mi
    if len(lows) >= 2:
        l1, l2 = lows[-2], lows[-1]
        diff_pct = abs(l1["price"] - l2["price"]) / max(l1["price"], 1)
        gap = l2["i"] - l1["i"]
        if diff_pct < 0.04 and gap >= 10:
            # İki dip arasındaki tepe → boyun çizgisi
            mid_highs = [h for h in highs if l1["i"] < h["i"] < l2["i"]]
            if mid_highs:
                neckline = max(h["price"] for h in mid_highs)
                pattern = {
                    "kind": "İkili Dip",
                    "low1": l1,
                    "low2": l2,
                    "neckline": neckline,
                    "target": neckline + (neckline - min(l1["price"], l2["price"])),
                }

    # İkili tepe de aynı simetriyle bak
    if not pattern and len(highs) >= 2:
        h1, h2 = highs[-2], highs[-1]
        diff_pct = abs(h1["price"] - h2["price"]) / max(h1["price"], 1)
        gap = h2["i"] - h1["i"]
        if diff_pct < 0.04 and gap >= 10:
            mid_lows = [l for l in lows if h1["i"] < l["i"] < h2["i"]]
            if mid_lows:
                neckline = min(l["price"] for l in mid_lows)
                pattern = {
                    "kind": "İkili Tepe",
                    "low1": h1,  # isim "low1" kalıyor ama tepe
                    "low2": h2,
                    "neckline": neckline,
                    "target": neckline - (max(h1["price"], h2["price"]) - neckline),
                }

    annotations = []
    metrics = []
    if pattern:
        side = "low" if pattern["kind"] == "İkili Dip" else "high"
        annotations.append({"type": "pivot", "i": pattern["low1"]["i"], "price": pattern["low1"]["price"], "side": side, "label": "1. " + ("Dip" if side == "low" else "Tepe")})
        annotations.append({"type": "pivot", "i": pattern["low2"]["i"], "price": pattern["low2"]["price"], "side": side, "label": "2. " + ("Dip" if side == "low" else "Tepe")})
        annotations.append({
            "type": "neckline",
            "from_i": pattern["low1"]["i"],
            "to_i": pattern["low2"]["i"],
            "price": pattern["neckline"],
            "label": "Boyun Çizgisi",
        })
        confirmed = (pattern["kind"] == "İkili Dip" and current > pattern["neckline"]) or \
                    (pattern["kind"] == "İkili Tepe" and current < pattern["neckline"])
        metrics = [
            {"label": "Formasyon", "value": pattern["kind"]},
            {"label": "Boyun çizgisi", "value": _fmt(pattern["neckline"], decimals)},
            {"label": "Hedef projeksiyonu", "value": _fmt(pattern["target"], decimals)},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Durum", "value": "Boyun çizgisi kırıldı — formasyon aktif" if confirmed else "Boyun çizgisi henüz kırılmadı — onay bekliyor"},
            {"label": "Pivot arası", "value": f"{pattern['low2']['i'] - pattern['low1']['i']} bar"},
        ]
        headline = f"{cfg['label']} grafiğinde potansiyel {pattern['kind']} tespit edildi."
    else:
        metrics = [
            {"label": "Tespit", "value": "Son 200 bar içinde ikili dip/tepe yapısı bulunamadı"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        headline = f"{cfg['label']} için ikili dip/tepe yapısı şu an aktif değil."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20)},
        "found": pattern is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #6: Üçgen Kırılımı
# ---------------------------------------------------------------------------
def _detect_triangle(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    swings = ti.find_swings(bars, left=4, right=4)
    decimals = cfg["round"]
    current = closes[-1]

    # Son 80 bar penceresinde son 3+ high ve 3+ low pivotunu al
    win = len(bars) - 80
    recent_highs = [s for s in swings["highs"] if s["i"] >= win][-4:]
    recent_lows = [s for s in swings["lows"] if s["i"] >= win][-4:]

    pattern = None
    if len(recent_highs) >= 2 and len(recent_lows) >= 2:
        high_lr = ti.linear_regression([p["i"] for p in recent_highs], [p["price"] for p in recent_highs])
        low_lr = ti.linear_regression([p["i"] for p in recent_lows], [p["price"] for p in recent_lows])
        # Eğim işaretlerine göre üçgen türü
        hs = high_lr["slope"]
        ls = low_lr["slope"]
        h_now = hs * (len(bars) - 1) + high_lr["intercept"]
        l_now = ls * (len(bars) - 1) + low_lr["intercept"]
        # Eğimler birbirine yaklaşıyor mu?
        if h_now > l_now and (h_now - l_now) / max(h_now, 1) < 0.10:
            # Üçgen türü
            if abs(hs) < 1e-6 * max(abs(h_now), 1) and ls > 0:
                kind = "Yükselen Üçgen"
                direction = "yukarı"
            elif hs < 0 and abs(ls) < 1e-6 * max(abs(l_now), 1):
                kind = "Alçalan Üçgen"
                direction = "aşağı"
            elif hs < 0 and ls > 0:
                kind = "Simetrik Üçgen"
                direction = "belirsiz"
            else:
                kind = "Üçgen"
                direction = "belirsiz"

            width = h_now - l_now
            # Üçgen yüksekliği (başlangıçta): max-min in window
            window_bars = bars[-80:] if len(bars) > 80 else bars
            tri_height = max(b["h"] for b in window_bars) - min(b["l"] for b in window_bars)
            pattern = {
                "kind": kind,
                "direction": direction,
                "upper_now": h_now,
                "lower_now": l_now,
                "high_lr": high_lr,
                "low_lr": low_lr,
                "height": tri_height,
                "highs": recent_highs,
                "lows": recent_lows,
            }

    annotations = []
    metrics = []
    if pattern:
        # Üst ve alt çizgi
        start_i = min(pattern["highs"][0]["i"], pattern["lows"][0]["i"])
        end_i = len(bars) - 1
        annotations.append({
            "type": "trendline",
            "from_i": start_i,
            "from_price": pattern["high_lr"]["slope"] * start_i + pattern["high_lr"]["intercept"],
            "to_i": end_i,
            "to_price": pattern["upper_now"],
            "label": "Üst Sınır",
            "r2": pattern["high_lr"]["r2"],
        })
        annotations.append({
            "type": "trendline",
            "from_i": start_i,
            "from_price": pattern["low_lr"]["slope"] * start_i + pattern["low_lr"]["intercept"],
            "to_i": end_i,
            "to_price": pattern["lower_now"],
            "label": "Alt Sınır",
            "r2": pattern["low_lr"]["r2"],
        })
        for p in pattern["highs"]:
            annotations.append({"type": "pivot", "i": p["i"], "price": p["price"], "side": "high"})
        for p in pattern["lows"]:
            annotations.append({"type": "pivot", "i": p["i"], "price": p["price"], "side": "low"})

        target_up = pattern["upper_now"] + pattern["height"]
        target_down = pattern["lower_now"] - pattern["height"]

        metrics = [
            {"label": "Formasyon", "value": pattern["kind"]},
            {"label": "Beklenen yön", "value": pattern["direction"].capitalize()},
            {"label": "Üst sınır", "value": _fmt(pattern["upper_now"], decimals)},
            {"label": "Alt sınır", "value": _fmt(pattern["lower_now"], decimals)},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Yukarı hedef", "value": _fmt(target_up, decimals)},
            {"label": "Aşağı hedef", "value": _fmt(target_down, decimals)},
        ]
        headline = f"{cfg['label']} grafiğinde {pattern['kind']} tespit edildi (beklenen yön: {pattern['direction']})."
    else:
        metrics = [
            {"label": "Tespit", "value": "Son 80 bar içinde üçgen yapısı bulunamadı"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        headline = f"{cfg['label']} için üçgen yapısı şu an aktif değil."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20)},
        "found": pattern is not None,
    }


# ---------------------------------------------------------------------------
# TEACHING — karakter + senaryo (her teknik için)
# ---------------------------------------------------------------------------
def _teaching(technique: str, found: bool, cfg: dict, label: str) -> dict:
    """Karakter sahnesi + 3 adım hikaye. found=False ise 'klasik senaryo' anlatımı."""
    asset_name = label
    teachings = {
        "engulfing-reversal": {
            "character": "Can",
            "intro": (
                f"Can mum desenlerinin sözlüğüne tutkun bir trader. {asset_name} grafiğine bakıyor. "
                "Bugün hikayemiz, yutan formasyonun (engulfing) nasıl 'günlük karakterin tek günde değiştiğini' söylediği üzerine."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Düşüş trendi",
                    "body": (
                        "Önceki günlerde küçük kırmızı gövdeli mumlar arka arkaya geldi. Satıcılar "
                        "kontrolü elinde tutuyor; alıcılar tepki üretmeyi denedi ama her seferinde "
                        "satıcılar daha düşükten karşıladı. 'LH/LL yapısı' aktif — düşüş canlı."
                    ),
                },
                {
                    "title": "Sahne 2 — Yutan mumun gelişi",
                    "body": (
                        "Bir sabah açılış öncekinin altında, ama gün boyunca alıcılar fiyatı yukarı "
                        "ittiler. Kapanış öncekinin gövdesinin üstüne çıktı — gövde tam yutuldu. "
                        "Hacim ortalamadan belirgin yüksek. Bu tek mum, önceki günün karakterini sildi."
                    ),
                },
                {
                    "title": "Sahne 3 — Onay arayışı",
                    "body": (
                        "Can hemen pozisyon açmaz. 'Tek mum hipotez; sonraki mum karar' der. Ertesi gün "
                        "yeşil bir kapanış daha gelirse, dönüş senaryosu güçlenir. Stop, yutan mumun "
                        "altına konur — formasyon iptal olursa kayıp dar tutulur."
                    ),
                },
            ],
            "takeaway": (
                "Yutan formasyon, üç filtre tam olduğunda klasik bir dönüş habercisidir: "
                "(1) düşüş trendinin sonunda görünmesi, (2) gövdenin gerçekten yutması, (3) hacim teyidi. "
                "Tek bir mum güçlü ama yalnız değildir — sonraki teyit veya iptal eder."
            ),
        },
        "support-resistance-bounce": {
            "character": "Emre",
            "intro": (
                f"Emre destek/direnç bantlarını fiyatın 'hafıza bölgeleri' olarak okur. "
                f"{asset_name} grafiğinde son 6 ayda dönüş üreten seviyeleri tarıyor."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Bantları işaretle",
                    "body": (
                        "Emre, fiyatın en az 2-3 kez döndüğü yatay bölgeleri ayıklar. Bunlar 'bant' olarak "
                        "düşünülür — piksel hassasiyetinde değil. En yakın destek altta, en yakın direnç "
                        "üstte. İki tarafta da ne var, oraya bakılır."
                    ),
                },
                {
                    "title": "Sahne 2 — Yaklaşma",
                    "body": (
                        f"{asset_name} bugün destek bandına %3'ten yakın. Burada üç olasılık var: "
                        "(a) tepki alıp yukarı döner, (b) yatay sıkışır ve sonraki teste hazırlanır, "
                        "(c) kapanışla aşağı kırılır. Her senaryonun gözleminde mum şekli + hacim okunur."
                    ),
                },
                {
                    "title": "Sahne 3 — Tepki veya kırılım",
                    "body": (
                        "Emre tepki senaryosunda uzun alt fitilli mum + ertesi gün yeşil kapanış arar — "
                        "'destek tuttu' demektir. Kırılım senaryosunda gövde altta kapanır + hacim 1.5x üstüdür. "
                        "Üçüncü olasılık 'belirsiz' — bu durumda işlem yok, sadece izleme."
                    ),
                },
            ],
            "takeaway": (
                "Destek/direnç tek başına karar değil, koşullu beklenti üretir. Disiplinli trader "
                "bir senaryoya kilitlenmek yerine iki olasılığa da planını yazar; mum + hacim hangisini "
                "doğrularsa o yöne hareket eder."
            ),
        },
        "trendline-break": {
            "character": "Zeynep",
            "intro": (
                f"Zeynep trend takipçisidir — yapı kırılmadıkça trende inanır. {asset_name} "
                "grafiğinde aktif trend çizgisinin testini izliyor."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Çizginin çizimi",
                    "body": (
                        "Zeynep son birkaç swing noktasını birleştirip trend çizgisini çizer. İki nokta "
                        "hipotezdir; üçüncü temas çizgiyi 'piyasaca onaylanmış' yapar. R² yüksekse "
                        "çizgi temiz; düşükse çizgi şüpheli."
                    ),
                },
                {
                    "title": "Sahne 2 — Test geliyor",
                    "body": (
                        "Fiyat çizgiye yaklaşıyor. Zeynep iki senaryoya hazırlanır: (a) çizgiden tepki "
                        "alıp ana yön devam, (b) kapanışla çizginin diğer tarafına kırılır → trend yapısı "
                        "soru işareti. Tek bir test trendi yıkmaz; kapanış + hacim teyidi yıkar."
                    ),
                },
                {
                    "title": "Sahne 3 — Kırılım veya tutma",
                    "body": (
                        "Çizgi tutuyorsa Zeynep ana trend yönündeki pozisyona devam. Kırılım gerçekleşirse "
                        "(gövde diğer tarafta kapandı + hacim güçlü), eski çizgi polariteyle rol değiştirir; "
                        "ilk retest tepkisi karar zemini olur. Zeynep aceleciliği reddeder; kapanışı bekler."
                    ),
                },
            ],
            "takeaway": (
                "Trend çizgisi tek başına büyü değil; üç kanıt (üçüncü temas, kapanış teyidi, retest reaksiyonu) "
                "üzerine kurulu bir disiplindir. Trend takipçisi, çizgi yaşıyorken yön; çizgi kırıldığında "
                "iki kat dikkatlidir."
            ),
        },
        "head-shoulders": {
            "character": "Emre",
            "intro": (
                f"Emre formasyon avcısıdır. {asset_name} grafiğinde son aylarda olası bir Omuz-Baş-Omuz "
                "yapısı şekilleniyor — ve klasik bir dönüş hikayesi sahneleniyor."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Sol omuz ve baş",
                    "body": (
                        "Trend yukarı; ilk tepe (sol omuz) oluşur, geri çekilme gelir, sonra daha yüksek bir "
                        "tepe (baş) yapılır. Buraya kadar her şey normal yükseliş yapısı — HH/HL aktif."
                    ),
                },
                {
                    "title": "Sahne 2 — Sağ omuz oluşuyor",
                    "body": (
                        "İkinci geri çekilmeden sonra fiyat yeniden yukarı çıkar ama bu kez başı geçemez — "
                        "sol omuzla aynı seviyelerde durur. Bu yapısal bir uyarı: alıcılar yeni zirve "
                        "yapmadı. Boyun çizgisi (iki ara dipten geçen) hâlâ aşağıdadır."
                    ),
                },
                {
                    "title": "Sahne 3 — Boyun çizgisinin kırılması",
                    "body": (
                        "Emre 'formasyon henüz onaylanmadı' der; boyun çizgisi günlük kapanışla aşağı "
                        "kırılırsa H&S devreye girer. Hedef projeksiyon: baş ile boyun çizgisi arasındaki "
                        "mesafe aşağı eklenir. Tutmazsa formasyon iptal — sağ omuz başın üstüne çıkarsa "
                        "yapısı tamamen geçersiz."
                    ),
                },
            ],
            "takeaway": (
                "H&S, klasik trend dönüşü hikâyesinin görselidir: alıcı baskısı zirve denemesinde "
                "tükendi. Formasyonun bilgi taşıması için boyun çizgisi kırılımı + hacim teyidi şarttır. "
                "Tamamlanmamış H&S sadece 'şüphe sinyali'dir; karar değil."
            ),
        },
        "double-bottom": {
            "character": "Selin",
            "intro": (
                f"Selin formasyon ve geometri bağını sever. {asset_name} grafiğinde aynı dipten iki kez "
                "dönüş tespit etti — klasik bir 'reddedilen ekstrem' yapısı."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — İlk dip",
                    "body": (
                        "Düşüş trendi içinde fiyat belirli bir seviyeye kadar indi, alıcılar tabandan "
                        "fiyatı geri çıkardı. Bu ilk dip tek başına 'tepki' demek; trend dönüşü değil."
                    ),
                },
                {
                    "title": "Sahne 2 — Geri çekilme ve ikinci dip",
                    "body": (
                        "Tepkinin ardından fiyat yeniden aynı bölgeye doğru indi. İki dip TAM aynı "
                        "seviyede olmaz — birbirine yakın (genelde %2-4 fark) olması yeterlidir. "
                        "Aynı tabandan ikinci dönüş, satıcıların yorulduğunu söyler."
                    ),
                },
                {
                    "title": "Sahne 3 — Boyun çizgisinin kırılması",
                    "body": (
                        "Selin iki dip arasındaki tepeyi 'boyun çizgisi' olarak çizer. Bu seviye yukarı "
                        "kırılırsa, double-bottom tamamlanır ve hedef projeksiyon yukarı yönde hesaplanır "
                        "(iki dip ile boyun çizgisi arası yükseklik kadar). Kırılım olmazsa formasyon "
                        "sadece potansiyel olarak kalır — karar yok."
                    ),
                },
            ],
            "takeaway": (
                "İkili dip, 'aynı kayalığa iki kez çarpan gemi rotayı değiştirir' sezgisinin grafik "
                "karşılığıdır. İki başarısız aşağı deneme + boyun çizgisi kırılımı = klasik düşüş→"
                "yükseliş dönüşü. Boyun çizgisi kırılmadan formasyon hipotez seviyesindedir."
            ),
        },
        "triangle-breakout": {
            "character": "Burak",
            "intro": (
                f"Burak saf price action takipçisidir. {asset_name} grafiğinde fiyat sıkışan bir üçgen "
                "yapısında — alıcı ve satıcı kararı yaklaşıyor."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Üçgenin oluşumu",
                    "body": (
                        "Burak son birkaç haftadaki tepe ve dipleri çizgiyle birleştirdi. İki çizgi "
                        "birbirine yaklaşıyor — fiyat sıkışıyor. Üst yatay + alçalan alt = yükselen "
                        "üçgen; yatay alt + alçalan üst = alçalan üçgen; ikisi de eğimli = simetrik."
                    ),
                },
                {
                    "title": "Sahne 2 — Sıkışmanın sonu",
                    "body": (
                        "Üçgenin sonuna yaklaştıkça hareket küçülür, hacim genelde düşer. Bu 'enerji "
                        "biriktirme' aşamasıdır — kırılım büyük olasılıkla ani ve hacimli olacak. "
                        "Burak iki tarafı da işaretler; hangi tarafa kapanış gelirse ona yönelir."
                    ),
                },
                {
                    "title": "Sahne 3 — Kırılım ve hedef",
                    "body": (
                        "Kırılım yönü açıklandığında Burak ölçülü hedefi hesaplar: üçgenin en geniş "
                        "noktasındaki yükseklik, kırılım noktasından projeksiyon olarak eklenir. "
                        "Stop, üçgenin diğer tarafına kalır. Sahte kırılımdan korunmak için kapanış "
                        "teyidi + hacim beklenir; sadece fitil değmesi yetmez."
                    ),
                },
            ],
            "takeaway": (
                "Üçgen formasyonu fiyatın bir 'karar noktasına' yaklaştığını söyler. Yönü tahmin etmek "
                "yerine, kırılımı bekleyip iki yöne de plan yazmak Burak'ın disiplinidir. Üçgen tipi "
                "(yükselen/alçalan/simetrik) yön ipucu verir ama garanti vermez — piyasa karar verir."
            ),
        },
    }
    return teachings.get(technique, {})


# ---------------------------------------------------------------------------
# Ana API
# ---------------------------------------------------------------------------
_TECHNIQUES = {
    "engulfing-reversal":         {"detect": _detect_engulfing,    "name": "Yutan Formasyon",       "default_asset": "BTC"},
    "support-resistance-bounce":  {"detect": _detect_sr_bounce,    "name": "Destek/Direnç Sıçraması", "default_asset": "BTC"},
    "trendline-break":            {"detect": _detect_trendline,    "name": "Trend Çizgisi Kırılımı",  "default_asset": "BTC"},
    "head-shoulders":             {"detect": _detect_hs,           "name": "Omuz-Baş-Omuz",         "default_asset": "BTC"},
    "double-bottom":              {"detect": _detect_double_bottom, "name": "İkili Dip / İkili Tepe", "default_asset": "BTC"},
    "triangle-breakout":          {"detect": _detect_triangle,     "name": "Üçgen Kırılımı",        "default_asset": "BTC"},
}

SUPPORTED_TECHNIQUES = sorted(_TECHNIQUES)
SUPPORTED_ASSETS = sorted(_ASSET_MAP)


def build_ta_example(technique: str, asset: Optional[str] = None) -> dict:
    """Bir TA tekniği için canlı örnek payload üretir.

    Returns dict: keys = technique, asset, source, timeframe, bars, indicators,
    annotations, summary, teaching, available, fetched_at, error?
    """
    if technique not in _TECHNIQUES:
        return {
            "available": False,
            "error": f"Desteklenmeyen teknik: {technique}",
            "supported": SUPPORTED_TECHNIQUES,
        }
    tcfg = _TECHNIQUES[technique]
    asset = (asset or tcfg["default_asset"]).upper()
    if asset not in _ASSET_MAP:
        return {
            "available": False,
            "error": f"Desteklenmeyen asset: {asset}",
            "supported": SUPPORTED_ASSETS,
        }
    acfg = _ASSET_MAP[asset]

    # Cache (technique+asset)
    cache_key = f"ta:{technique}:{asset}"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached["t"] < _CACHE_TTL:
        return cached["payload"]

    try:
        bars, source = _fetch_bars(asset)
    except Exception as e:
        logger.warning(f"TA bars fetch failed [{asset}]: {e}")
        return {
            "available": False,
            "technique": technique,
            "asset": asset,
            "error": str(e),
        }
    if not bars or len(bars) < 50:
        return {
            "available": False,
            "technique": technique,
            "asset": asset,
            "error": "Yetersiz veri",
        }

    detect_result = tcfg["detect"](bars, acfg)
    # Bar penceresini grafik için kırpalım (son 180)
    bars_window = _last_n_bars(bars, 180)
    # Annotation index'lerini yeni pencereye remap et
    offset = len(bars) - len(bars_window)
    for ann in detect_result["annotations"]:
        for key in ("i", "from_i", "to_i"):
            if key in ann and isinstance(ann[key], int):
                ann[key] = ann[key] - offset
                if ann[key] < 0:
                    ann[key] = 0

    # İndikator serilerini de pencereye sığdır
    indicators = {}
    for k, series in detect_result.get("indicators", {}).items():
        indicators[k] = series[-len(bars_window):] if series else []

    teaching = _teaching(technique, detect_result.get("found", False), acfg, acfg["label"])

    payload = {
        "available": True,
        "technique": technique,
        "technique_name": tcfg["name"],
        "asset": asset,
        "asset_label": acfg["label"],
        "currency": acfg["currency"],
        "decimals": max(acfg["round"], 0),
        "timeframe": "1d",
        "source": source,
        "bars": bars_window,
        "indicators": indicators,
        "annotations": detect_result["annotations"],
        "summary": {
            "headline": detect_result["headline"],
            "metrics": detect_result["metrics"],
            "found": detect_result.get("found", False),
        },
        "teaching": teaching,
        "supported_assets": SUPPORTED_ASSETS,
        "fetched_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
    }
    _CACHE[cache_key] = {"t": now, "payload": payload}
    return payload
