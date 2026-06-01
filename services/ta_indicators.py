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


def highs(bars: list[dict]) -> list[float]:
    return [b["h"] for b in bars]


def lows(bars: list[dict]) -> list[float]:
    return [b["l"] for b in bars]


def volumes(bars: list[dict]) -> list[float]:
    return [b.get("v", 0.0) for b in bars]


def last_value(series: Series) -> Optional[float]:
    for v in reversed(series):
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Stochastic Oscillator (%K, %D) — Lane klasiği
# ---------------------------------------------------------------------------
def stochastic(bars: list[dict], k_period: int = 14, d_period: int = 3) -> dict:
    """%K = (close - LL) / (HH - LL) × 100, sonra d_period SMA."""
    n = len(bars)
    k_series: Series = [None] * n
    for i in range(k_period - 1, n):
        window = bars[i - k_period + 1: i + 1]
        hh = max(b["h"] for b in window)
        ll = min(b["l"] for b in window)
        rng = hh - ll
        k_series[i] = ((bars[i]["c"] - ll) / rng * 100.0) if rng > 0 else 50.0
    # %D = %K'nın d_period SMA'sı
    d_series: Series = [None] * n
    for i in range(k_period + d_period - 2, n):
        vals = [k_series[j] for j in range(i - d_period + 1, i + 1) if k_series[j] is not None]
        if len(vals) == d_period:
            d_series[i] = sum(vals) / d_period
    return {"k": k_series, "d": d_series}


# ---------------------------------------------------------------------------
# Williams %R — Stochastic'in tersine işaretli kuzeni
# ---------------------------------------------------------------------------
def williams_r(bars: list[dict], period: int = 14) -> Series:
    """%R = (HH - close) / (HH - LL) × -100. -20 üstü aşırı alım, -80 altı aşırı satım."""
    n = len(bars)
    out: Series = [None] * n
    for i in range(period - 1, n):
        window = bars[i - period + 1: i + 1]
        hh = max(b["h"] for b in window)
        ll = min(b["l"] for b in window)
        rng = hh - ll
        out[i] = ((hh - bars[i]["c"]) / rng * -100.0) if rng > 0 else -50.0
    return out


# ---------------------------------------------------------------------------
# CCI (Commodity Channel Index) — Lambert orijinal
# ---------------------------------------------------------------------------
def cci(bars: list[dict], period: int = 20) -> Series:
    """CCI = (TP - SMA(TP)) / (0.015 × meanDeviation). +100 üstü güçlü, -100 altı zayıf."""
    n = len(bars)
    tp = [(b["h"] + b["l"] + b["c"]) / 3.0 for b in bars]
    out: Series = [None] * n
    for i in range(period - 1, n):
        window = tp[i - period + 1: i + 1]
        sma_tp = sum(window) / period
        mean_dev = sum(abs(x - sma_tp) for x in window) / period
        if mean_dev > 0:
            out[i] = (tp[i] - sma_tp) / (0.015 * mean_dev)
    return out


# ---------------------------------------------------------------------------
# ADX (Average Directional Index) — trend gücü (Wilder)
# ---------------------------------------------------------------------------
def adx(bars: list[dict], period: int = 14) -> dict:
    """ADX + DI+ + DI-. 25 üstü trend gücü, 20 altı yatay/zayıf."""
    n = len(bars)
    adx_s: Series = [None] * n
    di_plus: Series = [None] * n
    di_minus: Series = [None] * n
    if n < period * 2:
        return {"adx": adx_s, "di_plus": di_plus, "di_minus": di_minus}

    tr_l: list[float] = [bars[0]["h"] - bars[0]["l"]]
    dm_plus: list[float] = [0.0]
    dm_minus: list[float] = [0.0]
    for i in range(1, n):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        ph, pl = bars[i - 1]["h"], bars[i - 1]["l"]
        tr_l.append(max(h - l, abs(h - pc), abs(l - pc)))
        up = h - ph
        dn = pl - l
        dm_plus.append(up if (up > dn and up > 0) else 0.0)
        dm_minus.append(dn if (dn > up and dn > 0) else 0.0)

    # İlk Wilder smoothing (sum N)
    tr_sm = sum(tr_l[1: period + 1])
    dmp_sm = sum(dm_plus[1: period + 1])
    dmm_sm = sum(dm_minus[1: period + 1])
    dx_list: list[float] = []
    for i in range(period, n):
        if i > period:
            tr_sm = tr_sm - tr_sm / period + tr_l[i]
            dmp_sm = dmp_sm - dmp_sm / period + dm_plus[i]
            dmm_sm = dmm_sm - dmm_sm / period + dm_minus[i]
        if tr_sm > 0:
            dip = (dmp_sm / tr_sm) * 100.0
            dim = (dmm_sm / tr_sm) * 100.0
            di_plus[i] = dip
            di_minus[i] = dim
            denom = dip + dim
            dx_list.append(abs(dip - dim) / denom * 100.0 if denom > 0 else 0.0)
        else:
            dx_list.append(0.0)
    # ADX = DX'in Wilder smoothing'i (period)
    if len(dx_list) >= period:
        seed = sum(dx_list[:period]) / period
        idx0 = period + period - 1
        if idx0 < n:
            adx_s[idx0] = seed
        prev = seed
        for j in range(period, len(dx_list)):
            prev = (prev * (period - 1) + dx_list[j]) / period
            tgt = period + j
            if tgt < n:
                adx_s[tgt] = prev
    return {"adx": adx_s, "di_plus": di_plus, "di_minus": di_minus}


# ---------------------------------------------------------------------------
# OBV (On-Balance Volume) — Granville
# ---------------------------------------------------------------------------
def obv(bars: list[dict]) -> Series:
    n = len(bars)
    out: Series = [None] * n
    if n == 0:
        return out
    out[0] = 0.0
    cum = 0.0
    for i in range(1, n):
        if bars[i]["c"] > bars[i - 1]["c"]:
            cum += bars[i].get("v", 0.0)
        elif bars[i]["c"] < bars[i - 1]["c"]:
            cum -= bars[i].get("v", 0.0)
        out[i] = cum
    return out


# ---------------------------------------------------------------------------
# CMF (Chaikin Money Flow)
# ---------------------------------------------------------------------------
def cmf(bars: list[dict], period: int = 20) -> Series:
    n = len(bars)
    out: Series = [None] * n
    # MFM × Volume, ardından period rolling sum / vol rolling sum
    mfv: list[float] = []
    for b in bars:
        rng = b["h"] - b["l"]
        if rng > 0:
            mfm = ((b["c"] - b["l"]) - (b["h"] - b["c"])) / rng
        else:
            mfm = 0.0
        mfv.append(mfm * b.get("v", 0.0))
    vols = [b.get("v", 0.0) for b in bars]
    for i in range(period - 1, n):
        v_sum = sum(vols[i - period + 1: i + 1])
        mfv_sum = sum(mfv[i - period + 1: i + 1])
        out[i] = (mfv_sum / v_sum) if v_sum > 0 else 0.0
    return out


# ---------------------------------------------------------------------------
# MFI (Money Flow Index) — hacimli RSI
# ---------------------------------------------------------------------------
def mfi(bars: list[dict], period: int = 14) -> Series:
    n = len(bars)
    out: Series = [None] * n
    if n <= period:
        return out
    tp = [(b["h"] + b["l"] + b["c"]) / 3.0 for b in bars]
    rmf = [tp[i] * bars[i].get("v", 0.0) for i in range(n)]
    for i in range(period, n):
        pos = 0.0
        neg = 0.0
        for j in range(i - period + 1, i + 1):
            if j == 0:
                continue
            if tp[j] > tp[j - 1]:
                pos += rmf[j]
            elif tp[j] < tp[j - 1]:
                neg += rmf[j]
        if neg == 0:
            out[i] = 100.0
        else:
            mr = pos / neg
            out[i] = 100.0 - 100.0 / (1.0 + mr)
    return out


# ---------------------------------------------------------------------------
# Rolling VWAP — gün-içi/rolling N bar yaklaşımı
# ---------------------------------------------------------------------------
def rolling_vwap(bars: list[dict], period: int = 20) -> Series:
    n = len(bars)
    out: Series = [None] * n
    tp = [(b["h"] + b["l"] + b["c"]) / 3.0 for b in bars]
    pv = [tp[i] * bars[i].get("v", 0.0) for i in range(n)]
    vols = [b.get("v", 0.0) for b in bars]
    for i in range(period - 1, n):
        v_sum = sum(vols[i - period + 1: i + 1])
        pv_sum = sum(pv[i - period + 1: i + 1])
        if v_sum > 0:
            out[i] = pv_sum / v_sum
    return out


# ---------------------------------------------------------------------------
# Ichimoku Cloud — temel beş bileşen
# ---------------------------------------------------------------------------
def ichimoku(bars: list[dict], tenkan: int = 9, kijun: int = 26, senkou_b: int = 52) -> dict:
    """Tenkan=Donchian(9)/2, Kijun=Donchian(26)/2, Senkou A=(T+K)/2 ileri 26,
    Senkou B=Donchian(52)/2 ileri 26, Chikou=close geri 26.
    Senkou A/B uzantı kuyruğunda None ile temsil edilir (önümüze projeksiyon).
    """
    n = len(bars)
    def donchian_mid(period: int) -> Series:
        out: Series = [None] * n
        for i in range(period - 1, n):
            win = bars[i - period + 1: i + 1]
            hh = max(b["h"] for b in win)
            ll = min(b["l"] for b in win)
            out[i] = (hh + ll) / 2.0
        return out

    t = donchian_mid(tenkan)
    k = donchian_mid(kijun)
    sb = donchian_mid(senkou_b)
    sa: Series = [None] * n
    for i in range(n):
        if t[i] is not None and k[i] is not None:
            sa[i] = (t[i] + k[i]) / 2.0
    # Chikou: close[i] -> chikou[i-kijun] (geriye 26)
    chikou: Series = [None] * n
    for i in range(n):
        tgt = i - kijun
        if 0 <= tgt < n:
            chikou[tgt] = bars[i]["c"]
    return {
        "tenkan": t,
        "kijun": k,
        "senkou_a": sa,
        "senkou_b": sb,
        "chikou": chikou,
    }


# ---------------------------------------------------------------------------
# Divergence detector (klasik) — fiyat ve osilatör arasında
# ---------------------------------------------------------------------------
def detect_divergence(
    bars: list[dict],
    oscillator: Series,
    lookback: int = 60,
    pivot_window: int = 3,
) -> Optional[dict]:
    """Son `lookback` bar içinde klasik divergence ara.

    Bullish: fiyat LL (lower low) yaparken osilatör HL (higher low) → tabanda uyumsuzluk.
    Bearish: fiyat HH yaparken osilatör LH → tepede uyumsuzluk.
    Pivotlar `pivot_window` left/right ile bulunur.

    Geri dönen None ise bulunmadı; yoksa {kind, p1, p2, o1, o2} dict.
    """
    n = len(bars)
    start = max(pivot_window, n - lookback)
    # Pivot lows & highs (price)
    price_lows: list[dict] = []
    price_highs: list[dict] = []
    for i in range(start, n - pivot_window):
        l = bars[i]["l"]
        h = bars[i]["h"]
        if all(bars[j]["l"] > l for j in range(i - pivot_window, i + pivot_window + 1) if j != i):
            price_lows.append({"i": i, "price": l})
        if all(bars[j]["h"] < h for j in range(i - pivot_window, i + pivot_window + 1) if j != i):
            price_highs.append({"i": i, "price": h})

    # Bullish divergence — son iki dip
    if len(price_lows) >= 2:
        l1, l2 = price_lows[-2], price_lows[-1]
        o1 = oscillator[l1["i"]] if l1["i"] < len(oscillator) else None
        o2 = oscillator[l2["i"]] if l2["i"] < len(oscillator) else None
        if o1 is not None and o2 is not None:
            # Fiyat daha düşük dip, osilatör daha yüksek dip
            if l2["price"] < l1["price"] and o2 > o1:
                return {
                    "kind": "bullish",
                    "p1_i": l1["i"], "p1_price": l1["price"], "o1": o1,
                    "p2_i": l2["i"], "p2_price": l2["price"], "o2": o2,
                }

    # Bearish divergence — son iki tepe
    if len(price_highs) >= 2:
        h1, h2 = price_highs[-2], price_highs[-1]
        o1 = oscillator[h1["i"]] if h1["i"] < len(oscillator) else None
        o2 = oscillator[h2["i"]] if h2["i"] < len(oscillator) else None
        if o1 is not None and o2 is not None:
            if h2["price"] > h1["price"] and o2 < o1:
                return {
                    "kind": "bearish",
                    "p1_i": h1["i"], "p1_price": h1["price"], "o1": o1,
                    "p2_i": h2["i"], "p2_price": h2["price"], "o2": o2,
                }
    return None


# ---------------------------------------------------------------------------
# MACD crossover signal — sinyal kesişimi tespit
# ---------------------------------------------------------------------------
def macd_crossover(macd_line: Series, signal_line: Series, lookback: int = 30) -> Optional[dict]:
    """Son `lookback` bar içinde MACD line × signal line kesişimini bul.

    Bullish cross: line önceki barda altta, mevcut barda üstte.
    Bearish cross: line önceki barda üstte, mevcut barda altta.
    Sıfır çizgisinin altında/üstündeki kesişim ek bilgi (zero_side).
    """
    n = len(macd_line)
    start = max(1, n - lookback)
    for i in range(n - 1, start, -1):
        m_now, m_prev = macd_line[i], macd_line[i - 1]
        s_now, s_prev = signal_line[i], signal_line[i - 1]
        if None in (m_now, m_prev, s_now, s_prev):
            continue
        # Bullish
        if m_prev <= s_prev and m_now > s_now:
            return {
                "kind": "bullish",
                "i": i,
                "macd": m_now,
                "signal": s_now,
                "zero_side": "above" if m_now >= 0 else "below",
            }
        if m_prev >= s_prev and m_now < s_now:
            return {
                "kind": "bearish",
                "i": i,
                "macd": m_now,
                "signal": s_now,
                "zero_side": "above" if m_now >= 0 else "below",
            }
    return None


# ---------------------------------------------------------------------------
# Bollinger Squeeze — bant genişliği yüzdelik dilim
# ---------------------------------------------------------------------------
def detect_bb_squeeze(
    bb_width: Series,
    lookback: int = 120,
    percentile: float = 0.20,
) -> Optional[dict]:
    """Bant genişliği son `lookback` barın en dar %`percentile` diliminde mi?

    True dönerse 'squeeze' aktif — enerji birikiyor; sonraki kırılım büyük olabilir.
    """
    n = len(bb_width)
    if n == 0:
        return None
    last = bb_width[-1]
    if last is None:
        return None
    window = [v for v in bb_width[-lookback:] if v is not None]
    if len(window) < 30:
        return None
    sorted_w = sorted(window)
    threshold = sorted_w[int(len(sorted_w) * percentile)]
    is_squeeze = last <= threshold
    return {
        "active": is_squeeze,
        "width_now": last,
        "threshold": threshold,
        "min_in_window": sorted_w[0],
        "median": sorted_w[len(sorted_w) // 2],
    }


# ---------------------------------------------------------------------------
# Golden/Death Cross — kısa MA × uzun MA kesişimi
# ---------------------------------------------------------------------------
def detect_ma_cross(
    short_ma: Series,
    long_ma: Series,
    lookback: int = 60,
) -> Optional[dict]:
    """Klasik 50/200 SMA kesişimi (veya verilen iki MA). Son `lookback` içinde bul."""
    n = len(short_ma)
    start = max(1, n - lookback)
    for i in range(n - 1, start, -1):
        s_now, s_prev = short_ma[i], short_ma[i - 1]
        l_now, l_prev = long_ma[i], long_ma[i - 1]
        if None in (s_now, s_prev, l_now, l_prev):
            continue
        if s_prev <= l_prev and s_now > l_now:
            return {"kind": "golden", "i": i, "short": s_now, "long": l_now}
        if s_prev >= l_prev and s_now < l_now:
            return {"kind": "death", "i": i, "short": s_now, "long": l_now}
    return None
