"""AXIOM Teknik Analiz Akademisi — saf Python indikator motoru.

Sıfır dış bağımlılık (yalnız stdlib math). Tüm seri-fonksiyonları None-aware:
warmup döneminde None döner, böylece UI/JSON tarafında "yetersiz veri" sade
bir nullable gösterimle ele alınır.

Veri tipi konvansiyonu:
  - close:  list[float]
  - bars:   list[dict] with keys t (epoch ms), o, h, l, c, v
  - Çıktı serileri her zaman girdiyle AYNI uzunlukta. İlk N eleman None.

Felsefe: matematik şeffaf, debug edilebilir, deterministik. Aynı OHLC ile aynı
sonuç. Test yazılırken referans değerler TradingView/Investing.com ile
karşılaştırılabilir (Wilder smoothing varsayılan).
"""
from __future__ import annotations

import math
from typing import Optional

Series = list[Optional[float]]


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------
def sma(close: list[float], period: int) -> Series:
    """Simple Moving Average."""
    n = len(close)
    out: Series = [None] * n
    if period <= 0 or n < period:
        return out
    s = sum(close[:period])
    out[period - 1] = s / period
    for i in range(period, n):
        s += close[i] - close[i - period]
        out[i] = s / period
    return out


def ema(close: list[float], period: int) -> Series:
    """Exponential Moving Average. İlk değer = ilk N close'un SMA seed'i."""
    n = len(close)
    out: Series = [None] * n
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(close[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (close[i] - prev) * k + prev
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# RSI (Wilder smoothing — TradingView/Investing varsayılan)
# ---------------------------------------------------------------------------
def rsi(close: list[float], period: int = 14) -> Series:
    n = len(close)
    out: Series = [None] * n
    if period <= 0 or n <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = close[i] - close[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period

    rs = (avg_gain / avg_loss) if avg_loss > 0 else math.inf
    out[period] = 100.0 if math.isinf(rs) else 100.0 - 100.0 / (1.0 + rs)

    for i in range(period + 1, n):
        diff = close[i] - close[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = (avg_gain / avg_loss) if avg_loss > 0 else math.inf
        out[i] = 100.0 if math.isinf(rs) else 100.0 - 100.0 / (1.0 + rs)
    return out


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------
def macd(close: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD line + signal + histogram. Hepsi None-aware seri."""
    n = len(close)
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    line: Series = [None] * n
    for i in range(n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            line[i] = fast_ema[i] - slow_ema[i]

    sig: Series = [None] * n
    hist: Series = [None] * n
    valid_start = next((i for i, v in enumerate(line) if v is not None), None)
    if valid_start is not None and (n - valid_start) >= signal:
        # signal EMA'sını line'ın geçerli kuyruğu üzerinde hesapla
        tail = [v for v in line[valid_start:] if v is not None]
        tail_sig = ema(tail, signal)
        for j, v in enumerate(tail_sig):
            sig[valid_start + j] = v
        for i in range(n):
            if line[i] is not None and sig[i] is not None:
                hist[i] = line[i] - sig[i]
    return {"line": line, "signal": sig, "histogram": hist}


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------
def bollinger(close: list[float], period: int = 20, std_dev: float = 2.0) -> dict:
    n = len(close)
    mid = sma(close, period)
    upper: Series = [None] * n
    lower: Series = [None] * n
    width: Series = [None] * n
    for i in range(period - 1, n):
        m = mid[i]
        if m is None:
            continue
        window = close[i - period + 1: i + 1]
        variance = sum((x - m) ** 2 for x in window) / period
        sd = math.sqrt(variance)
        upper[i] = m + std_dev * sd
        lower[i] = m - std_dev * sd
        width[i] = ((upper[i] - lower[i]) / m) if m > 0 else None
    return {"upper": upper, "middle": mid, "lower": lower, "width": width}


# ---------------------------------------------------------------------------
# ATR (Average True Range, Wilder)
# ---------------------------------------------------------------------------
def atr(bars: list[dict], period: int = 14) -> Series:
    n = len(bars)
    out: Series = [None] * n
    if n < period + 1:
        return out

    trs: list[float] = [bars[0]["h"] - bars[0]["l"]]
    for i in range(1, n):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    init = sum(trs[1: period + 1]) / period
    out[period] = init
    prev = init
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


# ---------------------------------------------------------------------------
# Swing high/low detector — formasyon tespiti için temel
# ---------------------------------------------------------------------------
def find_swings(bars: list[dict], left: int = 5, right: int = 5) -> dict:
    """Pivot tespiti.

    bars[i].h, [i-left .. i+right] aralığındaki diğer h'lerin TÜMÜNDEN
    KATIYEN büyükse swing-high. Swing-low simetrik (low ile).
    """
    highs: list[dict] = []
    lows: list[dict] = []
    n = len(bars)
    for i in range(left, n - right):
        h = bars[i]["h"]
        l = bars[i]["l"]
        is_high = all(bars[j]["h"] < h for j in range(i - left, i + right + 1) if j != i)
        is_low = all(bars[j]["l"] > l for j in range(i - left, i + right + 1) if j != i)
        if is_high:
            highs.append({"i": i, "price": h, "t": bars[i].get("t")})
        if is_low:
            lows.append({"i": i, "price": l, "t": bars[i].get("t")})
    return {"highs": highs, "lows": lows}


# ---------------------------------------------------------------------------
# Fibonacci retracement & extension
# ---------------------------------------------------------------------------
_FIB_RETR = (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0)
_FIB_EXT = (1.272, 1.414, 1.618, 2.0, 2.618)


def fibonacci_retracement(swing_high: float, swing_low: float) -> list[dict]:
    """Yükseliş trendi düzeltmesi varsayar (swing_high > swing_low).

    0.000 = swing_high (yüzde sıfır geri çekilme)
    1.000 = swing_low  (tam geri çekilme)
    Aksi durumda da matematiksel olarak doğru çalışır.
    """
    diff = swing_high - swing_low
    out = []
    for r in _FIB_RETR:
        out.append({
            "ratio": r,
            "price": swing_high - diff * r,
            "label": f"{r:.3f}",
        })
    return out


def fibonacci_extension(swing_high: float, swing_low: float, retracement_end: float) -> list[dict]:
    """Geri çekilme bittikten sonraki uzantı (yukarı yön) hedefleri."""
    diff = swing_high - swing_low
    out = []
    for r in _FIB_EXT:
        out.append({
            "ratio": r,
            "price": retracement_end + diff * r,
            "label": f"{r:.3f}",
        })
    return out


# ---------------------------------------------------------------------------
# Linear regression — trend çizgisi
# ---------------------------------------------------------------------------
def linear_regression(xs: list[float], ys: list[float]) -> dict:
    """y = slope*x + intercept. R² beraber döner. Min 2 nokta."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return {"slope": 0.0, "intercept": 0.0, "r2": 0.0}
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    den = sum((xs[i] - mx) ** 2 for i in range(n))
    if den == 0:
        return {"slope": 0.0, "intercept": my, "r2": 0.0}
    slope = num / den
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((ys[i] - (slope * xs[i] + intercept)) ** 2 for i in range(n))
    r2 = (1 - ss_res / ss_tot) if ss_tot > 0 else 1.0
    return {"slope": slope, "intercept": intercept, "r2": r2}


# ---------------------------------------------------------------------------
# Mum desen helper'ları (formasyon motorunda kullanılır)
# ---------------------------------------------------------------------------
def candle_body(bar: dict) -> float:
    return abs(bar["c"] - bar["o"])


def candle_range(bar: dict) -> float:
    return bar["h"] - bar["l"]


def is_bullish(bar: dict) -> bool:
    return bar["c"] > bar["o"]


def is_bearish(bar: dict) -> bool:
    return bar["c"] < bar["o"]


def is_bullish_engulfing(prev: dict, curr: dict) -> bool:
    """Yutan boğa: önceki ayı, bugünkü boğa ve gövdesi önceki gövdeyi kapatır."""
    return (
        is_bearish(prev)
        and is_bullish(curr)
        and curr["c"] >= prev["o"]
        and curr["o"] <= prev["c"]
        and candle_body(curr) > candle_body(prev)
    )


def is_bearish_engulfing(prev: dict, curr: dict) -> bool:
    return (
        is_bullish(prev)
        and is_bearish(curr)
        and curr["o"] >= prev["c"]
        and curr["c"] <= prev["o"]
        and candle_body(curr) > candle_body(prev)
    )


def is_hammer(bar: dict, ratio: float = 2.0) -> bool:
    """Çekiç: küçük gövde, uzun alt fitil (>= ratio × gövde), küçük üst fitil."""
    body = candle_body(bar)
    if body <= 0:
        return False
    upper = bar["h"] - max(bar["o"], bar["c"])
    lower = min(bar["o"], bar["c"]) - bar["l"]
    return lower >= ratio * body and upper <= body


def is_shooting_star(bar: dict, ratio: float = 2.0) -> bool:
    body = candle_body(bar)
    if body <= 0:
        return False
    upper = bar["h"] - max(bar["o"], bar["c"])
    lower = min(bar["o"], bar["c"]) - bar["l"]
    return upper >= ratio * body and lower <= body


def is_doji(bar: dict, body_pct: float = 0.1) -> bool:
    """Doji: gövde, toplam menzilin %body_pct'inden küçük."""
    rng = candle_range(bar)
    if rng <= 0:
        return False
    return candle_body(bar) / rng <= body_pct


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def closes(bars: list[dict]) -> list[float]:
    return [b["c"] for b in bars]


def last_value(series: Series) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return v
    return None
