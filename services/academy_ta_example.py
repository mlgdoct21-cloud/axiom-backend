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
# 365 bar — SMA200 ve uzun-vade indikator için sağlam tarihçe (golden/death cross dahil)
_DEFAULT_BARS = 365

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
# DETECT — Teknik #7: Fibonacci Retracement
# ---------------------------------------------------------------------------
def _detect_fibonacci_retracement(bars: list[dict], cfg: dict) -> dict:
    """Son 120 barda en geniş swing low → swing high'ı bul, retracement çiz."""
    closes = ti.closes(bars)
    decimals = cfg["round"]
    current = closes[-1]

    # Son 120 barlık pencerede en derin swing low ve en yüksek swing high
    window = bars[-120:] if len(bars) > 120 else bars
    offset = len(bars) - len(window)
    swings = ti.find_swings(window, left=4, right=4)
    if not swings["highs"] or not swings["lows"]:
        return {
            "annotations": [],
            "metrics": [
                {"label": "Tespit", "value": "Yeterli swing noktası yok"},
                {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            ],
            "headline": f"{cfg['label']} için Fibonacci çizimi şu an mümkün değil.",
            "indicators": {"ema20": ti.ema(closes, 20)},
            "found": False,
        }

    # En yüksek swing high ve en düşük swing low (yön belirler)
    highest = max(swings["highs"], key=lambda s: s["price"])
    lowest = min(swings["lows"], key=lambda s: s["price"])

    # Yön: hangisi daha sonra geldiyse (timestamp/i bazlı)
    bullish_move = highest["i"] > lowest["i"]
    if bullish_move:
        swing_high = highest["price"]
        swing_low = lowest["price"]
        start_i = lowest["i"] + offset
        end_i = highest["i"] + offset
        direction = "yükseliş"
    else:
        # Düşüş hareketi: yüksek önce, düşük sonra
        swing_high = highest["price"]
        swing_low = lowest["price"]
        start_i = highest["i"] + offset
        end_i = lowest["i"] + offset
        direction = "düşüş"

    fibs = ti.fibonacci_retracement(swing_high, swing_low)

    # En yakın seviye
    nearest = min(fibs, key=lambda f: abs(f["price"] - current))
    dist_pct = abs(current - nearest["price"]) / current * 100

    annotations = []
    for f in fibs:
        annotations.append({
            "type": "fib_level",
            "price": f["price"],
            "label": f"{f['ratio']:.3f}",
            "ratio": f["ratio"],
        })
    # Swing pivotları
    annotations.append({"type": "pivot", "i": start_i, "price": swing_low if bullish_move else swing_high, "side": "low" if bullish_move else "high", "label": "Swing Başlangıcı"})
    annotations.append({"type": "pivot", "i": end_i, "price": swing_high if bullish_move else swing_low, "side": "high" if bullish_move else "low", "label": "Swing Sonu"})

    metrics = [
        {"label": "Yön", "value": direction.capitalize()},
        {"label": "Swing Low", "value": _fmt(swing_low, decimals)},
        {"label": "Swing High", "value": _fmt(swing_high, decimals)},
        {"label": "0.382 seviyesi", "value": _fmt(fibs[2]["price"], decimals)},
        {"label": "0.500 seviyesi", "value": _fmt(fibs[3]["price"], decimals)},
        {"label": "0.618 (altın oran)", "value": _fmt(fibs[4]["price"], decimals)},
        {"label": "0.786 son kale", "value": _fmt(fibs[5]["price"], decimals)},
        {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
        {"label": "En yakın Fib", "value": f"{nearest['label']} ({dist_pct:.1f}% mesafe)"},
    ]
    headline = f"{cfg['label']} {direction} hareketinde retracement; şu an {nearest['label']} seviyesine yakın."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20)},
        "found": True,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #8: RSI Divergence
# ---------------------------------------------------------------------------
def _detect_rsi_divergence(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    decimals = cfg["round"]
    rsi14 = ti.rsi(closes, 14)
    current = closes[-1]

    div = ti.detect_divergence(bars, rsi14, lookback=80, pivot_window=4)

    annotations = []
    metrics = []
    if div:
        annotations.append({"type": "pivot", "i": div["p1_i"], "price": div["p1_price"], "side": "low" if div["kind"] == "bullish" else "high", "label": "Pivot 1"})
        annotations.append({"type": "pivot", "i": div["p2_i"], "price": div["p2_price"], "side": "low" if div["kind"] == "bullish" else "high", "label": "Pivot 2"})
        annotations.append({
            "type": "divergence_line",
            "from_i": div["p1_i"],
            "from_price": div["p1_price"],
            "to_i": div["p2_i"],
            "to_price": div["p2_price"],
            "label": f"Fiyat {div['kind'].title()} Divergence",
            "kind": div["kind"],
        })

        metrics = [
            {"label": "Divergence türü", "value": "Bullish (klasik dönüş adayı)" if div["kind"] == "bullish" else "Bearish (klasik dönüş adayı)"},
            {"label": "Pivot 1 fiyat", "value": _fmt(div["p1_price"], decimals)},
            {"label": "Pivot 1 RSI", "value": f"{div['o1']:.1f}"},
            {"label": "Pivot 2 fiyat", "value": _fmt(div["p2_price"], decimals)},
            {"label": "Pivot 2 RSI", "value": f"{div['o2']:.1f}"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Anlık RSI", "value": f"{rsi14[-1]:.1f}" if rsi14[-1] else "—"},
        ]
        headline = (
            f"{cfg['label']} grafiğinde klasik {('bullish' if div['kind'] == 'bullish' else 'bearish')} "
            f"RSI divergence tespit edildi — momentum tükeniyor sinyali."
        )
    else:
        metrics = [
            {"label": "Tespit", "value": "Son 80 bar içinde belirgin klasik divergence yok"},
            {"label": "Anlık RSI(14)", "value": f"{rsi14[-1]:.1f}" if rsi14[-1] else "—"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        headline = f"{cfg['label']} için şu an aktif klasik divergence yok — sözlüksel örnek."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20), "rsi14": rsi14},
        "found": div is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #9: MACD Crossover
# ---------------------------------------------------------------------------
def _detect_macd_crossover(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    decimals = cfg["round"]
    m = ti.macd(closes, 12, 26, 9)
    cross = ti.macd_crossover(m["line"], m["signal"], lookback=40)
    current = closes[-1]

    annotations = []
    metrics = []
    if cross:
        bars_ago = len(bars) - 1 - cross["i"]
        annotations.append({
            "type": "marker",
            "i": cross["i"],
            "price": bars[cross["i"]]["c"],
            "side": "low" if cross["kind"] == "bullish" else "high",
            "label": f"{cross['kind'].title()} Cross",
        })
        # Sıfır çizgisi referansı
        metrics = [
            {"label": "Kesişim", "value": f"{cross['kind'].title()} ({bars_ago} bar önce)"},
            {"label": "Sıfır çizgisi", "value": "Üstünde (güçlü)" if cross["zero_side"] == "above" else "Altında (zayıf 'tepki' sinyali)"},
            {"label": "MACD line", "value": f"{cross['macd']:+.4f}" if abs(cross['macd']) < 10 else f"{cross['macd']:+.2f}"},
            {"label": "Signal line", "value": f"{cross['signal']:+.4f}" if abs(cross['signal']) < 10 else f"{cross['signal']:+.2f}"},
            {"label": "Histogram (şimdi)", "value": f"{m['histogram'][-1]:+.4f}" if m['histogram'][-1] is not None else "—"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        sign_qual = "güçlü trend devam" if cross["zero_side"] == "above" and cross["kind"] == "bullish" else (
            "zayıf 'tepki' sinyali" if cross["zero_side"] == "below" and cross["kind"] == "bullish" else (
                "güçlü düşüş devam" if cross["zero_side"] == "below" and cross["kind"] == "bearish" else
                "yorgun zirveden zayıf bearish"
            )
        )
        headline = f"{cfg['label']} grafiğinde MACD {cross['kind']} cross ({bars_ago} bar önce) — {sign_qual}."
    else:
        last_m = m["line"][-1]
        last_s = m["signal"][-1]
        metrics = [
            {"label": "Tespit", "value": "Son 40 bar içinde MACD-Signal kesişimi yok"},
            {"label": "MACD line (şimdi)", "value": f"{last_m:+.4f}" if last_m is not None else "—"},
            {"label": "Signal line (şimdi)", "value": f"{last_s:+.4f}" if last_s is not None else "—"},
            {"label": "Histogram (şimdi)", "value": f"{m['histogram'][-1]:+.4f}" if m['histogram'][-1] is not None else "—"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        headline = f"{cfg['label']} için son 40 barda MACD kesişimi yok — mevcut yapı korunuyor."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20), "macd_line": m["line"], "macd_signal": m["signal"], "macd_hist": m["histogram"]},
        "found": cross is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #10: Golden / Death Cross (50/200 SMA)
# ---------------------------------------------------------------------------
def _detect_golden_cross(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    decimals = cfg["round"]
    current = closes[-1]
    sma50 = ti.sma(closes, 50)
    sma200 = ti.sma(closes, 200)

    cross = ti.detect_ma_cross(sma50, sma200, lookback=120)

    # Eğer henüz cross yok ama 200 SMA mevcut → mevcut dizilim
    annotations = []
    metrics = []
    if cross:
        bars_ago = len(bars) - 1 - cross["i"]
        annotations.append({
            "type": "marker",
            "i": cross["i"],
            "price": bars[cross["i"]]["c"],
            "side": "low" if cross["kind"] == "golden" else "high",
            "label": f"{'Golden' if cross['kind'] == 'golden' else 'Death'} Cross",
        })
        metrics = [
            {"label": "Kesişim", "value": f"{'Golden Cross (altın haç)' if cross['kind'] == 'golden' else 'Death Cross (ölüm haçı)'}"},
            {"label": "Kesişim ne zaman", "value": f"{bars_ago} bar önce"},
            {"label": "SMA50 (şimdi)", "value": _fmt(sma50[-1], decimals) if sma50[-1] is not None else "—"},
            {"label": "SMA200 (şimdi)", "value": _fmt(sma200[-1], decimals) if sma200[-1] is not None else "—"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        msg = "ana trend yükselişe döndü" if cross["kind"] == "golden" else "ana trend düşüşe döndü"
        headline = f"{cfg['label']} grafiğinde {'Golden' if cross['kind'] == 'golden' else 'Death'} Cross ({bars_ago} bar önce) — {msg}."
    else:
        if sma50[-1] is not None and sma200[-1] is not None:
            dizilim = "SMA50 > SMA200 (yükseliş dizilimi)" if sma50[-1] > sma200[-1] else "SMA50 < SMA200 (düşüş dizilimi)"
            gap_pct = (sma50[-1] - sma200[-1]) / sma200[-1] * 100
            metrics = [
                {"label": "Kesişim", "value": "Son 120 bar içinde 50/200 kesişimi yok"},
                {"label": "Mevcut dizilim", "value": dizilim},
                {"label": "SMA50 (şimdi)", "value": _fmt(sma50[-1], decimals)},
                {"label": "SMA200 (şimdi)", "value": _fmt(sma200[-1], decimals)},
                {"label": "Fark", "value": f"{gap_pct:+.1f}%"},
                {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            ]
            headline = f"{cfg['label']} için kesişim yok; mevcut dizilim: {dizilim.lower()}."
        else:
            metrics = [
                {"label": "Tespit", "value": "SMA200 için yeterli veri yok (en az 200 bar gerek)"},
                {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            ]
            headline = f"{cfg['label']} için SMA200 verisi yetersiz."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"sma50": sma50, "sma200": sma200},
        "found": cross is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #11: Bollinger Squeeze
# ---------------------------------------------------------------------------
def _detect_bollinger_squeeze(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    decimals = cfg["round"]
    current = closes[-1]
    bb = ti.bollinger(closes, 20, 2.0)
    squeeze = ti.detect_bb_squeeze(bb["width"], lookback=120, percentile=0.20)

    annotations = []
    metrics = []
    if squeeze:
        # Üst ve alt bant son seviyelerini level olarak işaretle
        if bb["upper"][-1] is not None:
            annotations.append({"type": "level", "price": bb["upper"][-1], "label": "Üst Bant", "side": "resistance"})
        if bb["lower"][-1] is not None:
            annotations.append({"type": "level", "price": bb["lower"][-1], "label": "Alt Bant", "side": "support"})
        if bb["middle"][-1] is not None:
            annotations.append({"type": "level", "price": bb["middle"][-1], "label": "Orta (SMA20)", "side": "neutral"})

        active = squeeze["active"]
        width_now = squeeze["width_now"] * 100
        threshold = squeeze["threshold"] * 100
        median = squeeze["median"] * 100
        metrics = [
            {"label": "Squeeze aktif mi?", "value": "EVET — sıkışma içerisinde" if active else "HAYIR — bantlar açılmış"},
            {"label": "Bant genişliği (şimdi)", "value": f"{width_now:.2f}%"},
            {"label": "Squeeze eşiği (alt %20)", "value": f"{threshold:.2f}%"},
            {"label": "Medyan genişlik", "value": f"{median:.2f}%"},
            {"label": "Üst bant", "value": _fmt(bb['upper'][-1], decimals) if bb['upper'][-1] else "—"},
            {"label": "Orta bant (SMA20)", "value": _fmt(bb['middle'][-1], decimals) if bb['middle'][-1] else "—"},
            {"label": "Alt bant", "value": _fmt(bb['lower'][-1], decimals) if bb['lower'][-1] else "—"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
        ]
        if active:
            headline = f"{cfg['label']} Bollinger squeeze AKTİF — enerji birikiyor, yakın kırılım adayı (yön belirsiz)."
        else:
            headline = f"{cfg['label']} bantlar açılmış — squeeze yok, mevcut volatilite normal seviyede."
    else:
        metrics = [
            {"label": "Tespit", "value": "Bollinger genişlik geçmişi için yetersiz veri"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
        ]
        headline = f"{cfg['label']} için Bollinger squeeze analizi şu an mümkün değil."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20), "bb_upper": bb["upper"], "bb_middle": bb["middle"], "bb_lower": bb["lower"]},
        "found": squeeze is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #12: Volume Pop (hacim teyitli mum)
# ---------------------------------------------------------------------------
def _detect_volume_pop(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    decimals = cfg["round"]
    current = closes[-1]

    n = len(bars)
    # Son 30 barda hacim ortalamanın 1.8x üstüne çıkan ilk barı bul
    found_bar = None
    for i in range(n - 1, max(n - 30, 20), -1):
        recent_vols = [b.get("v", 0.0) for b in bars[max(0, i - 20):i] if b.get("v", 0.0) > 0]
        if not recent_vols:
            continue
        avg = sum(recent_vols) / len(recent_vols)
        cur_v = bars[i].get("v", 0.0)
        if avg > 0 and cur_v / avg >= 1.8:
            found_bar = {
                "i": i,
                "vol": cur_v,
                "avg": avg,
                "ratio": cur_v / avg,
                "bullish": bars[i]["c"] > bars[i]["o"],
                "range_pct": (bars[i]["h"] - bars[i]["l"]) / bars[i]["o"] * 100 if bars[i]["o"] > 0 else 0,
            }
            break

    obv_series = ti.obv(bars)
    cmf_series = ti.cmf(bars, 20)

    annotations = []
    metrics = []
    if found_bar:
        bar = bars[found_bar["i"]]
        bars_ago = n - 1 - found_bar["i"]
        annotations.append({
            "type": "marker",
            "i": found_bar["i"],
            "price": bar["c"],
            "side": "low" if found_bar["bullish"] else "high",
            "label": f"Volume Pop ({found_bar['ratio']:.1f}x)",
        })
        side_text = "Yeşil (alıcı baskısı)" if found_bar["bullish"] else "Kırmızı (satıcı baskısı)"
        metrics = [
            {"label": "Tespit", "value": f"Hacim pop ({bars_ago} bar önce)"},
            {"label": "Hacim oranı", "value": f"{found_bar['ratio']:.1f}x ortalama"},
            {"label": "Mum yönü", "value": side_text},
            {"label": "Mum menzili", "value": f"%{found_bar['range_pct']:.1f}"},
            {"label": "OBV (şimdi)", "value": f"{obv_series[-1]:,.0f}" if obv_series[-1] is not None else "—"},
            {"label": "CMF(20) (şimdi)", "value": f"{cmf_series[-1]:+.3f}" if cmf_series[-1] is not None else "—"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        teyit = "alıcı baskısı teyitli" if found_bar["bullish"] else "satıcı baskısı teyitli"
        headline = f"{cfg['label']} grafiğinde hacim pop ({found_bar['ratio']:.1f}x, {bars_ago} bar önce) — {teyit}."
    else:
        recent_vols = [b.get("v", 0.0) for b in bars[-21:-1] if b.get("v", 0.0) > 0]
        avg = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        cur_v = bars[-1].get("v", 0.0)
        metrics = [
            {"label": "Tespit", "value": "Son 30 barda anormal hacim popu yok"},
            {"label": "Bugünkü hacim", "value": f"{cur_v:,.0f}" if cur_v else "—"},
            {"label": "20-bar ortalama", "value": f"{avg:,.0f}" if avg else "—"},
            {"label": "Oran", "value": f"{cur_v/avg:.2f}x" if avg > 0 else "—"},
            {"label": "OBV (şimdi)", "value": f"{obv_series[-1]:,.0f}" if obv_series[-1] is not None else "—"},
            {"label": "CMF(20) (şimdi)", "value": f"{cmf_series[-1]:+.3f}" if cmf_series[-1] is not None else "—"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
        ]
        headline = f"{cfg['label']} için son 30 barda 1.8x üstü hacim popu yok — akış normal seyirde."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20), "obv": obv_series, "cmf20": cmf_series},
        "found": found_bar is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #13: Pivot Points (Faz 3 — advance)
# ---------------------------------------------------------------------------
def _detect_pivot_points(bars: list[dict], cfg: dict) -> dict:
    closes = ti.closes(bars)
    decimals = cfg["round"]
    if len(bars) < 2:
        return {
            "annotations": [],
            "metrics": [{"label": "Tespit", "value": "Yetersiz bar"}],
            "headline": f"{cfg['label']} için yeterli geçmiş yok.",
            "indicators": {"ema20": ti.ema(closes, 20) if closes else []},
            "found": False,
        }
    prev = bars[-2]
    piv = ti.classic_pivots(prev["h"], prev["l"], prev["c"])
    current = closes[-1]
    last_i = len(bars) - 1

    levels = [
        ("R3", piv["R3"], "high"),
        ("R2", piv["R2"], "high"),
        ("R1", piv["R1"], "high"),
        ("P",  piv["P"],  "neutral"),
        ("S1", piv["S1"], "low"),
        ("S2", piv["S2"], "low"),
        ("S3", piv["S3"], "low"),
    ]

    annotations = [
        {
            "type": "level",
            "i": last_i,
            "price": price,
            "side": side,
            "label": f"{name}: {_fmt(price, decimals)}",
        }
        for name, price, side in levels
    ]

    # Fiyatın pivot konumu
    if current > piv["R1"]:
        if current > piv["R2"]:
            position = "R2 üzerinde — boğa esnekliği uçta"
        else:
            position = "R1 üzerinde — varsayılan yön boğa"
    elif current < piv["S1"]:
        if current < piv["S2"]:
            position = "S2 altında — ayı esnekliği uçta"
        else:
            position = "S1 altında — varsayılan yön ayı"
    elif current > piv["P"]:
        position = "P üstünde — hafif boğa eğilim"
    else:
        position = "P altında — hafif ayı eğilim"

    metrics = [
        {"label": "Önceki H/L/C", "value": f"{_fmt(prev['h'], decimals)} / {_fmt(prev['l'], decimals)} / {_fmt(prev['c'], decimals)}"},
        {"label": "P (Pivot)", "value": _fmt(piv["P"], decimals)},
        {"label": "R1 / S1", "value": f"{_fmt(piv['R1'], decimals)} / {_fmt(piv['S1'], decimals)}"},
        {"label": "R2 / S2", "value": f"{_fmt(piv['R2'], decimals)} / {_fmt(piv['S2'], decimals)}"},
        {"label": "R3 / S3", "value": f"{_fmt(piv['R3'], decimals)} / {_fmt(piv['S3'], decimals)}"},
        {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
        {"label": "Konum", "value": position},
        {"label": "Trend", "value": _trend_label(bars)},
    ]
    headline = (
        f"{cfg['label']} klasik pivot çapaları — fiyat {position}. "
        f"P={_fmt(piv['P'], decimals)}, R1={_fmt(piv['R1'], decimals)}, S1={_fmt(piv['S1'], decimals)}."
    )

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes, 20)},
        "found": True,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #14: Multi-Timeframe Snapshot (Faz 3 — advance)
# ---------------------------------------------------------------------------
def _detect_mtf_snapshot(bars: list[dict], cfg: dict) -> dict:
    """Aylık / Haftalık / Günlük trend skorlarını üretir.

    Günlük bars verili. Haftalık ~7d, aylık ~30d aggregate ediyoruz.
    """
    closes = ti.closes(bars)
    decimals = cfg["round"]
    current = closes[-1]

    daily = bars
    weekly = ti.aggregate_period(bars, 7)
    monthly = ti.aggregate_period(bars, 30)

    # Zaman dilimine göre ölçeklenmiş SMA/RSI periyotları
    score_d = ti.trend_score(daily, short_p=20, long_p=50, rsi_p=14, min_bars=50)
    score_w = ti.trend_score(weekly, short_p=10, long_p=20, rsi_p=14, min_bars=20)
    score_m = ti.trend_score(monthly, short_p=3, long_p=6, rsi_p=6, min_bars=8)

    parts = []
    total = 0
    aligned = 0
    for name, s in (("Aylık", score_m), ("Haftalık", score_w), ("Günlük", score_d)):
        if s is None:
            parts.append(f"{name}: —")
            continue
        sign = "+" if s["score"] > 0 else ("" if s["score"] == 0 else "")
        verdict_tr = {"trend_up": "boğa", "trend_down": "ayı", "range": "yatay"}.get(s["verdict"], s["verdict"])
        parts.append(f"{name}: {sign}{s['score']} ({verdict_tr})")
        total += s["score"]
        if abs(s["score"]) >= 2:
            aligned += 1

    if score_m and score_w and score_d:
        signs = [
            1 if s["score"] >= 2 else (-1 if s["score"] <= -2 else 0)
            for s in (score_m, score_w, score_d)
        ]
        pos = sum(1 for x in signs if x > 0)
        neg = sum(1 for x in signs if x < 0)
        if pos == 3:
            verdict_overall = "TAM YELKEN BOĞA — üç zaman dilimi pozitif hizalı"
        elif neg == 3:
            verdict_overall = "TAM YELKEN AYI — üç zaman dilimi negatif hizalı"
        elif pos == 2 and neg == 0:
            verdict_overall = "İHTİYATLI BOĞA — iki pusula pozitif, biri nötr"
        elif neg == 2 and pos == 0:
            verdict_overall = "İHTİYATLI AYI — iki pusula negatif, biri nötr"
        elif pos > 0 and neg > 0:
            verdict_overall = "ÇELİŞKİ — pusulalar zıt; demir at"
        else:
            verdict_overall = "NÖTR — net yön yok"
    else:
        verdict_overall = "Yeterli haftalık/aylık veri yok"

    metrics = [
        {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
        {"label": "Aylık skor", "value": f"{score_m['score']:+d} ({score_m['verdict']})" if score_m else "—"},
        {"label": "Haftalık skor", "value": f"{score_w['score']:+d} ({score_w['verdict']})" if score_w else "—"},
        {"label": "Günlük skor", "value": f"{score_d['score']:+d} ({score_d['verdict']})" if score_d else "—"},
        {"label": "Toplam", "value": f"{total:+d} / 9"},
        {"label": "Hizalama", "value": f"{aligned} / 3 zaman dilimi"},
        {"label": "Sentez", "value": verdict_overall},
        {"label": "Trend (günlük)", "value": _trend_label(bars)},
    ]

    # SMA50 günlük overlay
    sma50 = ti.sma(closes, 50)
    annotations = []
    if sma50 and sma50[-1] is not None:
        annotations.append({
            "type": "level",
            "i": len(bars) - 1,
            "price": sma50[-1],
            "side": "neutral",
            "label": f"SMA50: {_fmt(sma50[-1], decimals)}",
        })

    headline = f"{cfg['label']} MTF taraması — {verdict_overall}."

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {
            "ema20": ti.ema(closes, 20),
            "sma50": sma50,
        },
        "found": score_d is not None and score_w is not None and score_m is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #15: Wyckoff Range (spring / upthrust) (Faz 3 — advance)
# ---------------------------------------------------------------------------
def _detect_wyckoff_range(bars: list[dict], cfg: dict) -> dict:
    closes_list = ti.closes(bars)
    decimals = cfg["round"]
    current = closes_list[-1]

    # Range tespiti son 60 barda
    norm_bars = [{"high": b["h"], "low": b["l"], "close": b["c"], "open": b["o"]} for b in bars]
    rng = ti.detect_range(norm_bars, lookback=60, tolerance_pct=1.5)
    annotations = []
    if not rng:
        # Range yoksa, kullanıcıya geniş 60-bar HL bandı göster (eğitsel)
        seg = bars[-60:]
        top = max(b["h"] for b in seg)
        bot = min(b["l"] for b in seg)
        metrics = [
            {"label": "Tespit", "value": "Son 60 barda kalıcı yatay range yok"},
            {"label": "60-bar Yüksek", "value": _fmt(top, decimals)},
            {"label": "60-bar Düşük", "value": _fmt(bot, decimals)},
            {"label": "Genişlik", "value": f"%{(top-bot)/((top+bot)/2)*100:.1f}"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Trend", "value": _trend_label(bars)},
        ]
        annotations = [
            {"type": "level", "i": len(bars) - 1, "price": top, "side": "high",
             "label": f"60-bar Yüksek: {_fmt(top, decimals)}"},
            {"type": "level", "i": len(bars) - 1, "price": bot, "side": "low",
             "label": f"60-bar Düşük: {_fmt(bot, decimals)}"},
        ]
        headline = f"{cfg['label']} son 60 barda Wyckoff range yapısı yok — trendsel veya volatil rejim."
        return {
            "annotations": annotations,
            "metrics": metrics,
            "headline": headline,
            "indicators": {"ema20": ti.ema(closes_list, 20)},
            "found": False,
        }

    last_i = len(bars) - 1
    annotations = [
        {"type": "level", "i": last_i, "price": rng["top"], "side": "high",
         "label": f"Range Üst: {_fmt(rng['top'], decimals)}"},
        {"type": "level", "i": last_i, "price": rng["bot"], "side": "low",
         "label": f"Range Alt: {_fmt(rng['bot'], decimals)}"},
        {"type": "level", "i": last_i, "price": rng["mid"], "side": "neutral",
         "label": f"Range Orta: {_fmt(rng['mid'], decimals)}"},
    ]

    spring = ti.detect_wyckoff_spring(norm_bars, rng, scan_bars=20)
    upthrust = ti.detect_wyckoff_upthrust(norm_bars, rng, scan_bars=20)

    event = None
    if spring and upthrust:
        # En yenisi
        event = spring if spring["i"] >= upthrust["i"] else upthrust
    elif spring:
        event = spring
    elif upthrust:
        event = upthrust

    if event:
        bars_ago = last_i - event["i"]
        kind_tr = "Spring (akümülasyon Phase C)" if event["kind"] == "spring" else "Upthrust (distribüsyon Phase C)"
        annotations.append({
            "type": "marker",
            "i": event["i"],
            "price": event.get("low") or event.get("high"),
            "side": "low" if event["kind"] == "spring" else "high",
            "kind": "bullish" if event["kind"] == "spring" else "bearish",
            "label": kind_tr,
        })
        metrics = [
            {"label": "Range Üst", "value": _fmt(rng["top"], decimals)},
            {"label": "Range Alt", "value": _fmt(rng["bot"], decimals)},
            {"label": "Genişlik", "value": f"%{rng['width_pct']:.1f}"},
            {"label": "Üst temas", "value": f"{rng['top_touches']} kez"},
            {"label": "Alt temas", "value": f"{rng['bot_touches']} kez"},
            {"label": "Olay", "value": f"{kind_tr} ({bars_ago} bar önce)"},
            {"label": "Penetrasyon", "value": f"%{event['penetration_pct']:.2f}"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
        ]
        verdict_text = (
            "Phase C → D'ye geçiş bekleniyor; SOS (Sign of Strength) teyidi aranır."
            if event["kind"] == "spring"
            else "Distribüsyon Phase C → SOW (Sign of Weakness) izlenir."
        )
        headline = f"{cfg['label']} {kind_tr} tespit edildi ({bars_ago} bar önce). {verdict_text}"
    else:
        metrics = [
            {"label": "Range Üst", "value": _fmt(rng["top"], decimals)},
            {"label": "Range Alt", "value": _fmt(rng["bot"], decimals)},
            {"label": "Genişlik", "value": f"%{rng['width_pct']:.1f}"},
            {"label": "Üst temas", "value": f"{rng['top_touches']} kez"},
            {"label": "Alt temas", "value": f"{rng['bot_touches']} kez"},
            {"label": "Spring/Upthrust", "value": "Son 20 barda yok"},
            {"label": "Anlık fiyat", "value": _fmt(current, decimals)},
            {"label": "Phase", "value": "B (yapı kurulumu) ihtimali"},
        ]
        headline = (
            f"{cfg['label']} Wyckoff range yapısı aktif — Phase B (yapı kurma); "
            f"spring/upthrust tetiği henüz yok."
        )

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes_list, 20)},
        "found": event is not None,
    }


# ---------------------------------------------------------------------------
# DETECT — Teknik #16: ATR Stop Planner (Faz 3 — advance)
# ---------------------------------------------------------------------------
def _detect_atr_stop_planner(bars: list[dict], cfg: dict) -> dict:
    closes_list = ti.closes(bars)
    decimals = cfg["round"]
    current = closes_list[-1]

    # ATR fonksiyonu o/h/l/c formatını bekliyor — raw bars'ı doğrudan ver
    atr_series = ti.atr(bars, 14)
    last_atr = ti.last_value(atr_series)
    last_i = len(bars) - 1

    if not last_atr or last_atr <= 0:
        return {
            "annotations": [],
            "metrics": [{"label": "Tespit", "value": "ATR hesaplanamadı"}],
            "headline": f"{cfg['label']} için ATR henüz hesaplanamadı.",
            "indicators": {"ema20": ti.ema(closes_list, 20), "atr14": atr_series},
            "found": False,
        }

    long_plan = ti.atr_stop(current, last_atr, side="long", k=1.5)
    # Pozisyon boyutu örneği: hesap=$10000, risk=%1
    ps = ti.position_size(10000.0, 1.0, long_plan["risk_per_unit"])

    annotations = [
        {"type": "level", "i": last_i, "price": long_plan["entry"], "side": "neutral",
         "label": f"Giriş: {_fmt(long_plan['entry'], decimals)}"},
        {"type": "level", "i": last_i, "price": long_plan["stop"], "side": "low",
         "label": f"Stop (ATR×1.5): {_fmt(long_plan['stop'], decimals)}"},
        {"type": "level", "i": last_i, "price": long_plan["tp1"], "side": "high",
         "label": f"TP1 (+1R): {_fmt(long_plan['tp1'], decimals)}"},
        {"type": "level", "i": last_i, "price": long_plan["tp2"], "side": "high",
         "label": f"TP2 (+2R): {_fmt(long_plan['tp2'], decimals)}"},
        {"type": "level", "i": last_i, "price": long_plan["tp3"], "side": "high",
         "label": f"TP3 (+3R): {_fmt(long_plan['tp3'], decimals)}"},
    ]

    risk_per_unit = long_plan["risk_per_unit"]
    atr_pct = (last_atr / current) * 100 if current > 0 else 0

    metrics = [
        {"label": "Anlık fiyat (entry)", "value": _fmt(current, decimals)},
        {"label": "ATR(14)", "value": f"{_fmt(last_atr, decimals)} (%{atr_pct:.2f})"},
        {"label": "Stop (k=1.5)", "value": _fmt(long_plan["stop"], decimals)},
        {"label": "1R (risk/birim)", "value": _fmt(risk_per_unit, decimals)},
        {"label": "TP1 (+1R)", "value": _fmt(long_plan["tp1"], decimals)},
        {"label": "TP2 (+2R)", "value": _fmt(long_plan["tp2"], decimals)},
        {"label": "TP3 (+3R)", "value": _fmt(long_plan["tp3"], decimals)},
        {"label": "Örnek hesap ($10K @ %1)", "value": f"{ps['units']:.4f} birim (~${ps['risk_amount']:.0f} risk)"},
        {"label": "Trend", "value": _trend_label(bars)},
    ]
    headline = (
        f"{cfg['label']} canlı stop planlaması — entry {_fmt(current, decimals)}, "
        f"ATR(14)={_fmt(last_atr, decimals)}, k=1.5 stop {_fmt(long_plan['stop'], decimals)}. "
        f"TP1/2/3: {_fmt(long_plan['tp1'], decimals)} / {_fmt(long_plan['tp2'], decimals)} / {_fmt(long_plan['tp3'], decimals)}."
    )

    return {
        "annotations": annotations,
        "metrics": metrics,
        "headline": headline,
        "indicators": {"ema20": ti.ema(closes_list, 20), "atr14": atr_series},
        "found": True,
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
    # Faz 2 teknikleri
    teachings.update({
        "fibonacci-retracement": {
            "character": "Ayşe",
            "intro": (
                f"Ayşe Fibonacci aracını {asset_name} grafiğine çiziyor. Soru: kalabalığın "
                "koordinatlı durduğu seviyeler nerede? 0.382, 0.500, 0.618 ve 0.786 — bu dört "
                "rakam matematikle psikolojinin buluştuğu yerlerdir."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Swing'i seç",
                    "body": (
                        "Ayşe önce hareketin net olduğu en geniş swing low ve swing high'ı bulur. "
                        "Yanlış swing seçilirse seviyeler yanıltıcı olur. Doğru çizimde araç "
                        "kalabalığın hafızasıyla aynı koordinatları üretir."
                    ),
                },
                {
                    "title": "Sahne 2 — Derinliği yorumla",
                    "body": (
                        "0.382 = sağlam trend (sığ düzeltme), 0.500 = psikolojik orta, 0.618 = "
                        "derin ama trend hâlâ sağlam, 0.786 = son kale (altında trend hipotezi "
                        "kırılır). Ayşe fiyatın hangi seviyeye geldiğini görerek 'trend ne durumda' "
                        "sorusuna ön cevap yazar."
                    ),
                },
                {
                    "title": "Sahne 3 — Confluence ara",
                    "body": (
                        "Tek başına Fibonacci 'belki'. Aynı seviyede EMA50 + yatay destek + yuvarlak "
                        "sayı varsa, dört topluluk aynı yerde emir biriktirir — seviye 'mutlaka dikkat' "
                        "zonuna döner. Ayşe işlem kararını confluence kalitesine bağlar."
                    ),
                },
            ],
            "takeaway": (
                "Fibonacci 'sihir' değil; kalabalığın koordinatlı durduğu yerlerin haritasıdır. "
                "Tek seviye ihtimal, confluence yığını karar zemini. 0.786 altı yapı kırılması "
                "eşiğidir — orada dikkat iki kat artar."
            ),
        },
        "rsi-divergence": {
            "character": "Selin",
            "intro": (
                f"Selin {asset_name} grafiğinde RSI'ı açtı. Fiyat ve RSI ters yönde mi? Bu "
                "klasik divergence sorusu — 'trendin altındaki gizli yorgunluk' hikayesi."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Pivotları işaretle",
                    "body": (
                        "Selin önce fiyatın son iki belirgin dip/tepesini bulur. Sonra RSI'ın aynı "
                        "barlardaki değerini okur. Pivotları işaretlemeden divergence yorumu havada "
                        "kalır — hangi iki nokta karşılaştırılıyor netleşmelidir."
                    ),
                },
                {
                    "title": "Sahne 2 — Yön farkını yakala",
                    "body": (
                        "Fiyat yeni dip yapıyor ama RSI daha yüksek dip yapıyor → bullish klasik "
                        "divergence (taban dönüş adayı). Fiyat yeni tepe yapıyor ama RSI daha düşük "
                        "tepe yapıyor → bearish klasik divergence (tepe dönüş adayı). Tek başına "
                        "alım/satım değil — sadece dikkat zonu."
                    ),
                },
                {
                    "title": "Sahne 3 — Teyidi bekle",
                    "body": (
                        "Divergence görüldüğünde Selin hemen pozisyon açmaz. Mum şekli (çekiç/yıldız), "
                        "S/R tepkisi ve hacim teyidi aranır. Üç-dört kanıt birikmeden işlem yok — bu "
                        "divergence cluster'larında haftalarca yanlış sinyal almamanın yoludur."
                    ),
                },
            ],
            "takeaway": (
                "Divergence trendin altındaki sessiz tükenmeyi gösterir. Klasik divergence dönüş "
                "adayı, gizli divergence devam adayı — ikisini karıştırmak en sık hatadır. Tek "
                "başına işlem değil, çok-kanıt bekleyen 'dikkat sinyali'."
            ),
        },
        "macd-crossover": {
            "character": "Mehmet",
            "intro": (
                f"Mehmet {asset_name} grafiğinde MACD panelini açtı. İki çizgi kesişiyor mu? "
                "Sıfır çizgisinin neresinde? Bu iki soru cross sinyalinin kalitesini belirler."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Cross yönünü ayırt et",
                    "body": (
                        "MACD line, signal line'ı aşağıdan yukarı keserse 'bullish cross'; yukarıdan "
                        "aşağı keserse 'bearish cross'. Histogram cross'tan birkaç bar önce sıfıra "
                        "döner — Mehmet bu erken uyarıyı izler."
                    ),
                },
                {
                    "title": "Sahne 2 — Sıfır çizgisini oku",
                    "body": (
                        "Cross'un sıfır çizgisine göre konumu kritiktir. Sıfır üstü bullish cross = "
                        "'trend devam' sinyali (güçlü). Sıfır altı bullish cross = 'düşüş içinde tepki' "
                        "(zayıf). Konum, sinyal kalitesini değiştirir."
                    ),
                },
                {
                    "title": "Sahne 3 — Histogram ve teyit",
                    "body": (
                        "Mehmet cross sonrası histogramın büyüyüp büyümediğine bakar. Büyüyorsa "
                        "trend hızlanıyor; küçülüyorsa sinyal yalan adayı. Trend filtresi (EMA200) "
                        "ile çakışan cross'lar ek ağırlık alır."
                    ),
                },
            ],
            "takeaway": (
                "MACD cross tek başına 'al/sat' değil. Sıfır çizgisi mevkisi sinyal kalitesini "
                "belirler; histogram momentumun hızını gösterir. Trend ve sıfır pozisyonu eşleşen "
                "cross'lar klasik 'güçlü teyit' verir."
            ),
        },
        "golden-cross": {
            "character": "Zeynep",
            "intro": (
                f"Zeynep {asset_name} grafiğinde SMA50 ve SMA200'ü açtı. İki çizgi nerede? "
                "Kesişti mi? Bu klasik 'yatırımcı çizgileri' uzun-vade trendin habercisidir."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Dizilimi gör",
                    "body": (
                        "Önce mevcut dizilim: SMA50 > SMA200 ise yükseliş yapısı, tersi düşüş. "
                        "Aradaki fark büyüyorsa trend hızlanıyor; daralıyorsa cross yaklaşıyor "
                        "olabilir. Zeynep bu 'sıkışma'yı erken uyarı olarak kullanır."
                    ),
                },
                {
                    "title": "Sahne 2 — Cross olayı",
                    "body": (
                        "Golden Cross: SMA50 yukarıdan SMA200'ü keser → ana trend yükselişe döndü "
                        "habercisi. Death Cross: tam tersi → düşüşe döndü. Bu çizgiler gecikmelidir; "
                        "cross olduğunda hareketin bir kısmı kaçırılmıştır."
                    ),
                },
                {
                    "title": "Sahne 3 — Yorum ve plan",
                    "body": (
                        "Zeynep golden cross sonrası 'yatırımcı bias'ını yukarı çevirir; portföy "
                        "ağırlığı artırılır. Death cross sonrası risk azaltma, nakit ağırlığı artırma "
                        "klasik refleks. Bu çizgiler 'gün-içi karar' değil, 'ay-yıl politikası' aracıdır."
                    ),
                },
            ],
            "takeaway": (
                "Golden/Death Cross gecikmeli ama güvenilirdir. Yatay piyasada zayıf, trend "
                "piyasasında muhteşemdir. 'Yatırımcı çizgileri' adıyla anılır çünkü gün-içi "
                "değil, ana yön politikası için tasarlanmıştır."
            ),
        },
        "bollinger-squeeze": {
            "character": "Burak",
            "intro": (
                f"Burak {asset_name} grafiğinde Bollinger Bantları'nın daralıp daralmadığına bakıyor. "
                "Sessizlik fırtına öncüsü mü? Bu klasik 'enerji birikimi' sorusu."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Bant genişliğini izle",
                    "body": (
                        "Bantlar 20 SMA etrafında ±2 standart sapma. Sapma daralıyorsa volatilite "
                        "düşüyor → bantlar sıkışıyor. Burak son 120 barın bant genişliği yüzdelik "
                        "diliminde alt %20'ye geldiyse 'squeeze aktif' der."
                    ),
                },
                {
                    "title": "Sahne 2 — Yön bekleme",
                    "body": (
                        "Squeeze yön söylemez — sadece 'yakında büyük hareket' der. Burak iki "
                        "tarafa da plan yazar: üst bant kırılım + hacim güçlü → bullish; alt bant "
                        "kırılım + hacim güçlü → bearish. Yön piyasa kararıyla netleşir."
                    ),
                },
                {
                    "title": "Sahne 3 — Hedef projeksiyonu",
                    "body": (
                        "Kırılım gerçekleştiğinde hedef projeksiyon: squeeze sırasındaki en geniş "
                        "bant genişliği kadar kırılım yönüne yansıtılır. Stop, bantın diğer tarafına "
                        "kalır. Burak hacim teyidi olmadan kırılımı sahte sayar; bekler."
                    ),
                },
            ],
            "takeaway": (
                "Bollinger squeeze bir 'sessizlik fırtınası' örüntüsüdür. Yön sinyali değil, "
                "hareket sinyali. Daralma ne kadar uzun ve dar olursa, sonraki kırılım o kadar "
                "büyüktür; hacim teyidi olmadan kırılım sahte adayıdır."
            ),
        },
        "volume-pop": {
            "character": "Murat",
            "intro": (
                f"Murat hacme bakan bir trader. {asset_name} grafiğinde son barlarda anormal "
                "yüksek hacim var mı? Bu 'kim gerçekten taşıdı' sorusunun cevabı."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Anormal hacmi tespit et",
                    "body": (
                        "Murat son 20 barın ortalama hacmini hesaplar. Mevcut bar bunun 1.8x üstüne "
                        "çıktıysa 'volume pop' aktif. 1.5-2x güçlü, 3x+ blow-off, 0.5x altı zayıf "
                        "hareket — hacim oranı hareketin ağırlığını söyler."
                    ),
                },
                {
                    "title": "Sahne 2 — Yön ve mum yapısı",
                    "body": (
                        "Pop'un yönü mum yapısıyla okunur. Büyük yeşil + yüksek hacim = sağlam alıcı "
                        "baskısı. Büyük kırmızı + yüksek hacim = sağlam satıcı baskısı. Doji + yüksek "
                        "hacim = kararsızlık (climax adayı, dönüş öncüsü olabilir)."
                    ),
                },
                {
                    "title": "Sahne 3 — Akış teyidi",
                    "body": (
                        "Murat OBV ve CMF ile akış istikrarını doğrular. Pop bullish ve OBV "
                        "yeni zirvedeyse trend güçlü. Pop bullish ama OBV LH ise akış divergence — "
                        "sürpriz dönüş riski yüksek. Pop tek başına değil, akış bağlamıyla karar zemini."
                    ),
                },
            ],
            "takeaway": (
                "Hacim hareketin ağırlığıdır. Anormal pop bir 'kim gerçekten taşıdı' sinyalidir "
                "— ama yön mum yapısıyla, sürdürülebilirlik OBV/CMF akış teyidiyle okunur. "
                "Tek başına pop 'olay var' der, 'karar' demez."
            ),
        },
    })
    # Faz 3 — advance teknikler
    teachings.update({
        "pivot-points": {
            "character": "Mehmet",
            "intro": (
                f"Mehmet kurumsal masaların gün başı rutinini gözlemler. {asset_name} "
                "grafiğinde önceki periyodun H/L/C özetinden klasik pivotları üretiyor — sabah "
                "kahvesiyle birlikte gün için 'hangi seviye önemli' sorusunun matematiksel cevabı."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Pivot çapaları çiziliyor",
                    "body": (
                        f"Pivot formülü öğretmeni Mehmet'e şunu söyledi: P = (H+L+C)/3. {asset_name} "
                        "için dünkü kapanış, dünkü yüksek ve dünkü düşük artık çapa. R1 ve S1 ilk "
                        "katmandaki direnç/destek; R2/S2 esneme tonu; R3/S3 uç noktalar."
                    ),
                },
                {
                    "title": "Sahne 2 — Fiyatın günlük konumu",
                    "body": (
                        "Fiyat P üzerindeyse günün varsayılan yönü boğa; ilk hedef R1. P altındaysa "
                        "varsayılan yön ayı; ilk hedef S1. Mehmet 'pivot tek başına sinyal değil; "
                        "fiyatın pivota nasıl yaklaştığı, nasıl tutunduğu önemli' der."
                    ),
                },
                {
                    "title": "Sahne 3 — Confluence izi",
                    "body": (
                        "Mehmet pivot seviyelerini Fibonacci ve klasik S/R ile karşılaştırır. Bir "
                        "pivot başka çerçevelerle aynı fiyatta çakışıyorsa o seviye 'ağırlaşır'; "
                        "kurumsal akış böyle ortak çapalarda gerçekleşir."
                    ),
                },
            ],
            "takeaway": (
                "Pivot points, kurumsalın gün başı paylaştığı dilden bir parçadır. Tek başına "
                "karar üretmez; ama confluence ve fiyatın seviyelere reaksiyonu okunduğunda "
                "kalibrasyon çapasıdır. Yorum yok, formül var — bu mekaniğin gücü."
            ),
        },
        "multi-timeframe-snapshot": {
            "character": "Ayşe",
            "intro": (
                f"Ayşe karşı rüzgara yelken açtığını fark eden bir trader. Bu sefer {asset_name} "
                "üzerinde aylık, haftalık ve günlük üç pusulayı aynı anda kalibre ediyor."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Üst rüzgar yönü (aylık/haftalık)",
                    "body": (
                        "Önce büyük rota: aylık trend skoru. SMA20 vs SMA50, kapanış vs SMA50, RSI — "
                        "bu üç bileşen -3 ile +3 arası bir skor verir. Haftalık aynı formülle. Üst "
                        "rüzgar pozitifse alt zaman dilimi tetikleri 'gerçek tetik' olabilir."
                    ),
                },
                {
                    "title": "Sahne 2 — Tetik zaman dilimi (günlük)",
                    "body": (
                        "Günlük skor üst pusulalarla aynı yöndeyse 'tam yelken'. İki pozitif, biri "
                        "nötr ise 'ihtiyatlı yelken'. Çelişki varsa Ayşe demir atar — tetik gürültüden "
                        "ayrılamaz olduğunda işlem yapılmaz."
                    ),
                },
                {
                    "title": "Sahne 3 — Sentez kararı",
                    "body": (
                        "Üç skor toplandığında genel rota çıkar. Aylık +3, haftalık +2, günlük +3 → "
                        "tam yelken boğa. Aylık -2, haftalık +1, günlük +3 → çelişki, dikkat. AXIOM "
                        "Pusula Sinyali bu mantığı sayısallaştırır; trader kuralı uygular."
                    ),
                },
            ],
            "takeaway": (
                "MTF disiplini gürültü ile gerçek tetiği ayırır. Tek zaman dilimi 'belki', üç zaman "
                "dilimi 'rota'. Üst pusula yönüyle çelişen tetikler yok sayılır; bu kural %50 hit "
                "rate'i bile uzun vadede pozitif R'ye dönüştürür."
            ),
        },
        "wyckoff-range": {
            "character": "Zeynep",
            "intro": (
                f"Zeynep yatay sıkışmaları seven bir swing trader. {asset_name} grafiğinde Wyckoff "
                "akümülasyon/distribüsyon haritasının izlerini arıyor — Composite Man'in sessiz hikayesi."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — Range tespiti",
                    "body": (
                        "Zeynep son 60 barda fiyatın bir üst ve alt sınır arasında salındığını "
                        "doğrular: en az 2 üst temas + en az 2 alt temas + barların %75'i bant "
                        "içinde kapanıyor. Bu Wyckoff'un Phase A/B aşaması — yapı kuruluyor."
                    ),
                },
                {
                    "title": "Sahne 2 — Spring veya upthrust izi",
                    "body": (
                        "Range alt sınırının altına kısa süreliğine inip içeri toparlanan mum = spring "
                        "(akümülasyon Phase C). Range üst sınırının üstüne fitil çıkarıp gövde içeri "
                        "kapanan mum = upthrust (distribüsyon Phase C). Bu mum Composite Man'in 'son "
                        "satıcıyı temizleme' veya 'son alıcıyı kandırma' hareketidir."
                    ),
                },
                {
                    "title": "Sahne 3 — Phase D teyit beklentisi",
                    "body": (
                        "Spring sonrası Zeynep SOS (Sign of Strength) bekler — range üst sınırının "
                        "güçlü hacimle kırılması. Upthrust sonrası SOW (Sign of Weakness) — alt "
                        "sınırın aşağı kırılması. Tetik geldiğinde önceki range_width kadar projeksiyon "
                        "hedeftir."
                    ),
                },
            ],
            "takeaway": (
                "Wyckoff, range içindeki sessizliğin altındaki niyeti okumaktır. Spring tek başına "
                "long sinyali değildir; Phase D'de SOS ile teyit gerekir. Hikayeyi sabırla okuyan "
                "trader, kırılım anını piyangoya bırakmaz."
            ),
        },
        "atr-stop-planner": {
            "character": "Burak",
            "intro": (
                f"Burak setup'tan önce risk planını her zaman yazılı yapar. {asset_name} için "
                "ATR-temelli stop ve TP1/TP2/TP3 hedeflerini matematiksel olarak kalibre ediyor."
            ),
            "steps": [
                {
                    "title": "Sahne 1 — ATR rejim haritası",
                    "body": (
                        "ATR(14) son 14 barın gerçek menzilini ortalar — piyasanın 'normal nefes "
                        "alma' büyüklüğü. ATR fiyat oranı (% ATR) volatilite rejimini söyler: %1 sakin, "
                        "%3+ tetikte. Stop yüzde yerine ATR×k ile kalibre edilirse rejim körü olmaz."
                    ),
                },
                {
                    "title": "Sahne 2 — Stop ve R-multiple",
                    "body": (
                        "Burak entry = anlık fiyat, stop = entry - 1.5×ATR (long). 1R = entry - stop. "
                        "TP1/2/3 = entry + 1R/2R/3R. Bu disiplinde ne kazandığı ve ne kaybettiği R "
                        "cinsinden ölçülür; uzun vadede edge'i mutlak para birimi gizlemez."
                    ),
                },
                {
                    "title": "Sahne 3 — Pozisyon boyutlandırma",
                    "body": (
                        "Sabit-%R modelinde her işlem için account'un sabit bir yüzdesi (%1 yaygın) "
                        "riske atılır. units = (account × risk%) / risk_per_unit. Stop yakınsa pozisyon "
                        "büyür, uzaksa küçülür; risk her zaman sabit. Burak $10K hesap × %1 = $100 "
                        "risk ile setup başına kaç birim alacağını saniyede hesaplar."
                    ),
                },
            ],
            "takeaway": (
                "Risk planı yazılırsa, duygunun yeri kalmaz. ATR rejimi kalibre eder, %R sabit-edge'i "
                "korur, R-multiple uzun vadeli istatistiği görünür kılar. Edge stratejide değil, planı "
                "uygulama disiplininin oranındadır."
            ),
        },
    })
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
    # Faz 2
    "fibonacci-retracement":      {"detect": _detect_fibonacci_retracement, "name": "Fibonacci Geri Çekilme", "default_asset": "BTC"},
    "rsi-divergence":             {"detect": _detect_rsi_divergence,        "name": "RSI Divergence",         "default_asset": "BTC"},
    "macd-crossover":             {"detect": _detect_macd_crossover,        "name": "MACD Kesişimi",          "default_asset": "BTC"},
    "golden-cross":               {"detect": _detect_golden_cross,          "name": "Golden / Death Cross",   "default_asset": "BTC"},
    "bollinger-squeeze":          {"detect": _detect_bollinger_squeeze,     "name": "Bollinger Squeeze",      "default_asset": "BTC"},
    "volume-pop":                 {"detect": _detect_volume_pop,            "name": "Volume Pop",             "default_asset": "BTC"},
    # Faz 3 — advance
    "pivot-points":               {"detect": _detect_pivot_points,          "name": "Pivot Points (Klasik)",  "default_asset": "BTC"},
    "multi-timeframe-snapshot":   {"detect": _detect_mtf_snapshot,          "name": "MTF Pusula Sentezi",     "default_asset": "BTC"},
    "wyckoff-range":              {"detect": _detect_wyckoff_range,         "name": "Wyckoff Range & Spring", "default_asset": "BTC"},
    "atr-stop-planner":           {"detect": _detect_atr_stop_planner,      "name": "ATR Stop Planlayıcı",    "default_asset": "BTC"},
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
