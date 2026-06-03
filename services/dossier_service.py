"""Trade Dossier service — sembol başına AL/TUT/SAT sentezi.

Lean v3 Faz 3 — Mehmet'in kişisel trade aracı.

Akış:
  1. Sembol tipini tespit et (BIST | CRYPTO | US)
  2. Tip-spesifik veri blokları topla (TA + Temel + Haber + Makro)
  3. Tek Gemini 2.5 Flash çağrısı ile sentezle → yapılandırılmış JSON
  4. trade_dossiers tablosuna yaz; 30dk içinde tekrar istenirse cache döndür

Veri kaynakları:
  - TA: services/academy_ta_example._fetch_bars (crypto/US) + services/bist_service (BIST)
        + services/ta_indicators (RSI/MACD/BB/EMA/ATR/swing)
  - Temel: services/bist_financials_service.get_symbol_financials (BIST UFRS)
           + FMP balance-sheet/profile (US/crypto'da skip)
  - Haber: news_items WHERE symbol = X AND created_at >= NOW() - 7d
  - Makro: macro_releases recent + USDTRY/DXY proxy (best-effort)

Hata toleransı: her veri bloğu fail-soft (boşsa "veri yok" string'i). Gemini
çağrısı başarısızsa error kolonu doldurulur + boş şablon döner.
"""
from __future__ import annotations

import os
import re
import json
import time
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Any

import requests
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.database import AsyncSessionLocal
from core.logger import get_logger
from models.trade_dossier import TradeDossier
from services.market_detector import MarketDetector, BIST_SYMBOLS
from services import ta_indicators as ti

logger = get_logger("dossier_service")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_TIMEOUT = 45
CACHE_TTL_MIN = 30
FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()

_CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "MATIC", "DOT", "LINK", "ATOM", "UNI", "LTC", "ETC", "BCH", "TON", "TRX", "NEAR", "ARB", "OP", "SUI", "APT", "INJ"}


# ---------------------------------------------------------------------------
# Symbol type detection
# ---------------------------------------------------------------------------
def _detect_type(symbol: str) -> str:
    """BIST | CRYPTO | US — `services/market_detector.py` BIST + custom crypto list."""
    s = symbol.upper().strip()
    if s in BIST_SYMBOLS:
        return "BIST"
    if s in _CRYPTO_TICKERS or s.endswith("USDT") or s.endswith("-USD"):
        return "CRYPTO"
    return "US"


# ---------------------------------------------------------------------------
# Price / OHLC fetch (sync, çağıran asyncio.to_thread ile sarar)
# ---------------------------------------------------------------------------
def _fetch_bars_for_type(symbol: str, sym_type: str) -> list[dict]:
    """200 günlük OHLC. Boşsa [] döner."""
    try:
        if sym_type == "CRYPTO":
            base = symbol.replace("-USD", "").replace("USDT", "")
            pair = f"{base}USDT"
            url = "https://api.binance.com/api/v3/klines"
            r = requests.get(url, params={"symbol": pair, "interval": "1d", "limit": 200}, timeout=8)
            r.raise_for_status()
            return [
                {"t": int(row[0]), "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4]), "v": float(row[5])}
                for row in r.json()
            ]
        if sym_type == "US":
            if not FMP_API_KEY:
                return []
            url = f"https://financialmodelingprep.com/api/v3/historical-price-full/{symbol}"
            r = requests.get(url, params={"apikey": FMP_API_KEY, "timeseries": 200}, timeout=8)
            r.raise_for_status()
            raw = (r.json() or {}).get("historical", [])
            raw.reverse()
            bars = []
            for row in raw:
                try:
                    dt = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
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
        if sym_type == "BIST":
            # BIST: yfinance .IS sembolü
            import yfinance as yf  # type: ignore
            yf_sym = f"{symbol}.IS"
            tk = yf.Ticker(yf_sym)
            hist = tk.history(period="9mo", interval="1d", auto_adjust=False)
            if hist is None or hist.empty:
                return []
            bars = []
            for idx, row in hist.iterrows():
                try:
                    bars.append({
                        "t": int(idx.timestamp() * 1000),
                        "o": float(row["Open"]),
                        "h": float(row["High"]),
                        "l": float(row["Low"]),
                        "c": float(row["Close"]),
                        "v": float(row.get("Volume", 0) or 0),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
            return bars
    except Exception as e:
        logger.warning(f"Bars fetch hata {symbol}/{sym_type}: {e}")
    return []


# ---------------------------------------------------------------------------
# TA snapshot — ta_indicators wrap
# ---------------------------------------------------------------------------
def _last(seq) -> Optional[float]:
    if not seq:
        return None
    for v in reversed(seq):
        if v is not None:
            return float(v)
    return None


def _ta_snapshot(bars: list[dict]) -> dict:
    """Son barda TA göstergeleri. Boşsa {'empty': True}."""
    if len(bars) < 50:
        return {"empty": True, "reason": f"yetersiz veri ({len(bars)} bar)"}

    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    volumes = [b["v"] for b in bars]
    last_close = closes[-1]

    rsi_series = ti.rsi(closes, 14)
    macd_d = ti.macd(closes, 12, 26, 9)
    bb = ti.bollinger(closes, 20, 2.0)
    ema20 = ti.ema(closes, 20)
    ema50 = ti.ema(closes, 50)
    sma200 = ti.sma(closes, 200) if len(closes) >= 200 else None
    atr_series = ti.atr(bars, 14)

    # Swing high/low — son 60 bar
    recent60 = bars[-60:]
    swings = ti.find_swings(recent60, left=5, right=5) if hasattr(ti, "find_swings") else None
    swing_high = max((s.get("price", 0) for s in (swings.get("highs", []) if isinstance(swings, dict) else [])), default=None)
    swing_low = min((s.get("price", 0) for s in (swings.get("lows", []) if isinstance(swings, dict) else [])), default=None)
    if swing_high is None or swing_high == 0:
        swing_high = max(highs[-60:])
    if swing_low is None or swing_low == 0:
        swing_low = min(lows[-60:])

    # 200-bar performans
    perf_200 = ((last_close / closes[-200] - 1) * 100) if len(closes) >= 200 else None
    perf_30 = ((last_close / closes[-30] - 1) * 100) if len(closes) >= 30 else None
    perf_7 = ((last_close / closes[-7] - 1) * 100) if len(closes) >= 7 else None

    return {
        "last_close": round(last_close, 4),
        "rsi14": round(_last(rsi_series) or 0, 1),
        "macd_line": round(_last(macd_d.get("line", [])) or 0, 4),
        "macd_signal": round(_last(macd_d.get("signal", [])) or 0, 4),
        "macd_hist": round(_last(macd_d.get("histogram", [])) or 0, 4),
        "bb_upper": round(_last(bb.get("upper", [])) or 0, 4),
        "bb_middle": round(_last(bb.get("middle", [])) or 0, 4),
        "bb_lower": round(_last(bb.get("lower", [])) or 0, 4),
        "ema20": round(_last(ema20) or 0, 4),
        "ema50": round(_last(ema50) or 0, 4),
        "sma200": round(_last(sma200) or 0, 4) if sma200 else None,
        "atr14": round(_last(atr_series) or 0, 4),
        "swing_high_60d": round(swing_high, 4),
        "swing_low_60d": round(swing_low, 4),
        "perf_7d_pct": round(perf_7, 2) if perf_7 is not None else None,
        "perf_30d_pct": round(perf_30, 2) if perf_30 is not None else None,
        "perf_200d_pct": round(perf_200, 2) if perf_200 is not None else None,
        "above_ema50": (last_close > (_last(ema50) or last_close)),
        "above_sma200": (last_close > (_last(sma200) or last_close)) if sma200 else None,
    }


# ---------------------------------------------------------------------------
# Fundamental — BIST UFRS + FMP profile/balance-sheet
# ---------------------------------------------------------------------------
async def _fundamental_block(symbol: str, sym_type: str) -> dict:
    if sym_type == "BIST":
        try:
            from services.bist_financials_service import get_symbol_financials
            data = await get_symbol_financials(symbol)
            periods = data.get("periods", [])
            items = data.get("items", [])
            if not periods or not items:
                return {"empty": True, "reason": "BIST UFRS verisi yok (haftalık cron beklenir)"}
            latest = periods[-1] if periods else None
            # Önemli kalemleri seç
            keep_codes = {"1A", "1B", "1AA", "1BA", "2A", "2B", "3A", "3CA", "3C", "3CF", "3DA", "4A"}
            keep_names = {"Dönen Varlıklar", "Duran Varlıklar", "Toplam Varlıklar", "Kısa Vadeli Yükümlülükler", "Uzun Vadeli Yükümlülükler", "Özkaynaklar", "Hasılat", "Brüt Kar", "FAVÖK", "Net Kar"}
            picked = []
            for it in items:
                code = (it.get("item_code") or "").strip()
                name = (it.get("item_name_tr") or "").strip()
                if code in keep_codes or any(kn in name for kn in keep_names):
                    vals = it.get("values", {})
                    if latest and latest in vals:
                        picked.append({"code": code, "name": name, "latest_period": latest, "value": vals.get(latest)})
            return {"market": "BIST", "latest_period": latest, "currency": "TRY", "key_items": picked[:25]}
        except Exception as e:
            logger.warning(f"BIST fundamental hata {symbol}: {e}")
            return {"empty": True, "reason": str(e)}

    if sym_type == "US":
        if not FMP_API_KEY:
            return {"empty": True, "reason": "FMP_API_KEY yok"}
        try:
            base = "https://financialmodelingprep.com/api/v3"
            profile_r = await asyncio.to_thread(requests.get, f"{base}/profile/{symbol}", params={"apikey": FMP_API_KEY}, timeout=8)
            ratios_r = await asyncio.to_thread(requests.get, f"{base}/ratios-ttm/{symbol}", params={"apikey": FMP_API_KEY}, timeout=8)
            analyst_r = await asyncio.to_thread(requests.get, f"{base}/price-target-consensus", params={"symbol": symbol, "apikey": FMP_API_KEY}, timeout=8)
            profile = (profile_r.json() or [{}])[0] if profile_r.status_code == 200 else {}
            ratios = (ratios_r.json() or [{}])[0] if ratios_r.status_code == 200 else {}
            analyst = (analyst_r.json() or [{}])[0] if analyst_r.status_code == 200 else {}
            return {
                "market": "US",
                "company": profile.get("companyName"),
                "sector": profile.get("sector"),
                "industry": profile.get("industry"),
                "marketCap_usd": profile.get("mktCap"),
                "beta": profile.get("beta"),
                "pe_ttm": ratios.get("peRatioTTM"),
                "pb": ratios.get("priceToBookRatioTTM"),
                "ps": ratios.get("priceToSalesRatioTTM"),
                "roe_ttm": ratios.get("returnOnEquityTTM"),
                "roic_ttm": ratios.get("returnOnCapitalEmployedTTM"),
                "debt_to_equity": ratios.get("debtEquityRatioTTM"),
                "current_ratio": ratios.get("currentRatioTTM"),
                "gross_margin": ratios.get("grossProfitMarginTTM"),
                "net_margin": ratios.get("netProfitMarginTTM"),
                "dividend_yield": ratios.get("dividendYieldTTM"),
                "analyst_target_high": analyst.get("targetHigh"),
                "analyst_target_median": analyst.get("targetMedian"),
                "analyst_target_low": analyst.get("targetLow"),
                "analyst_target_consensus": analyst.get("targetConsensus"),
            }
        except Exception as e:
            logger.warning(f"FMP fundamental hata {symbol}: {e}")
            return {"empty": True, "reason": str(e)}

    # CRYPTO
    return {"market": "CRYPTO", "note": "kripto için klasik bilanço yok; akış/onchain ayrı blokta"}


# ---------------------------------------------------------------------------
# News — son 7 gün
# ---------------------------------------------------------------------------
async def _news_block(symbol: str) -> dict:
    try:
        async with AsyncSessionLocal() as db:
            sql = text("""
                SELECT id, source, original_title, axiom_analysis, dashboard_summary, created_at
                FROM news_items
                WHERE (symbol = :sym OR upper(original_title) LIKE :pat)
                  AND created_at >= NOW() - INTERVAL '7 days'
                ORDER BY created_at DESC
                LIMIT 15
            """)
            res = await db.execute(sql, {"sym": symbol.upper(), "pat": f"%{symbol.upper()}%"})
            rows = res.fetchall()
            if not rows:
                return {"empty": True, "reason": "son 7 günde haber yok"}
            items = []
            for r in rows:
                items.append({
                    "source": r[1],
                    "title": (r[2] or "")[:200],
                    "summary": (r[4] or r[3] or "")[:300],
                    "created_at": r[5].isoformat() if r[5] else None,
                })
            return {"count": len(items), "items": items}
    except Exception as e:
        logger.warning(f"News block hata {symbol}: {e}")
        return {"empty": True, "reason": str(e)}


# ---------------------------------------------------------------------------
# Macro — son 14 gün önemli olaylar + canlı çevirme oranları
# ---------------------------------------------------------------------------
async def _macro_block(sym_type: str) -> dict:
    try:
        async with AsyncSessionLocal() as db:
            sql = text("""
                SELECT event_type, country, scheduled_at, actual_value, surprise_pct, sentiment_score
                FROM macro_releases
                WHERE released_at IS NOT NULL
                  AND released_at >= NOW() - INTERVAL '14 days'
                ORDER BY released_at DESC
                LIMIT 12
            """)
            res = await db.execute(sql)
            rows = res.fetchall()
            recent = [
                {
                    "event": r[0],
                    "country": r[1],
                    "at": r[2].isoformat() if r[2] else None,
                    "actual": str(r[3]) if r[3] is not None else None,
                    "surprise_pct": float(r[4]) if r[4] is not None else None,
                    "sentiment": float(r[5]) if r[5] is not None else None,
                }
                for r in rows
            ]
        # FX referans
        fx = {}
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=5)
            if r.status_code == 200:
                fx["BTCUSDT"] = float(r.json().get("price", 0))
        except Exception:
            pass
        if sym_type == "BIST":
            # USDTRY için frankfurter.app (ücretsiz, key gerekmez)
            try:
                r = requests.get("https://api.frankfurter.app/latest", params={"from": "USD", "to": "TRY"}, timeout=5)
                if r.status_code == 200:
                    fx["USDTRY"] = float(r.json().get("rates", {}).get("TRY", 0))
            except Exception:
                pass
        return {"recent_events": recent, "fx": fx}
    except Exception as e:
        logger.warning(f"Macro block hata: {e}")
        return {"empty": True, "reason": str(e)}


# ---------------------------------------------------------------------------
# Gemini çağrısı
# ---------------------------------------------------------------------------
_DOSSIER_PROMPT_TEMPLATE = """Sen Mehmet'in kişisel trade analistisin. Görev: aşağıdaki verilere DAYANARAK Türkçe yapılandırılmış bir trade dossier üret.

KRITIK KURALLAR:
- SADECE verilen verileri kullan. Ezberden konuşma, jenerik tavsiye verme.
- "Sektör ortalaması X" gibi iddiada bulunmak için verilerde o veri OLMALI.
- Her drivers/risks maddesi SOMUT bir veriye (sayı, yüzde, seviye, tarih) referansla.
- "Olabilir / -ebilir" yerine somut tetik/seviye söyle.
- Risk maddeleri sembole/şirkete ÖZEL olmalı, "piyasa volatilitesi" gibi jenerik değil.

Sembol: {symbol} ({symbol_type})
Tarih: {now_iso}

=== TEKNİK GÖSTERGE SNAPSHOT (günlük) ===
{ta_block}

=== TEMEL VERİ ===
{fundamental_block}

=== SON 7 GÜN HABER AKIŞI ===
{news_block}

=== MAKRO BAĞLAM (son 14 gün önemli olaylar + FX) ===
{macro_block}

ÇIKTI (saf JSON, başka metin yazma):
{{
  "verdict": "AL" | "TUT" | "SAT",
  "conviction": 1-5 arası tam sayı,
  "thesis": "1-2 cümle ana tez — neden bu karar",
  "key_drivers": ["en az 3 madde, her biri somut veriden alıntı: rakam/yüzde/seviye/tarih"],
  "risks": ["3 risk — sembole özel, jenerik değil"],
  "trigger": "AL/SAT'ı TEYİT veya BOZACAK somut olay (örn: 'haftalık MA50 üstü kapanış', '650 TL direnç kırılımı', 'Q3 bilanço Hasılat <X')",
  "stop_level": "fiyat (sayı veya 'belirsiz')",
  "target_levels": [{{"level": "fiyat", "rationale": "neden bu seviye"}}],
  "horizon": "intraday" | "swing" | "position" | "long_term",
  "confidence_explainer": "conviction skorunun gerekçesi — hangi veriler net, hangileri eksik"
}}
"""


def _call_gemini(prompt: str) -> tuple[Optional[dict], Optional[str]]:
    """Gemini 2.5 Flash. (parsed_json, error_msg) döner."""
    if not GEMINI_API_KEY or "buraya" in GEMINI_API_KEY:
        return None, "GEMINI_API_KEY yok"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            # 2026-06-03: 1500→3000. Dossier prompt çok uzun (TA+Temel+Haber+Makro
            # snapshot'ları + verdict+thesis+key_drivers+risks+trigger+stop+targets
            # +horizon+confidence). 1500 token'da JSON ortasında kesiliyordu
            # (AVAX testi: thesis "... ve 8.103" kesik). Mehmet UX raporu.
            "maxOutputTokens": 3000,
            "responseMimeType": "application/json",
        },
    }
    try:
        r = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=GEMINI_TIMEOUT)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None, "no candidates"
        txt = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        if not txt:
            return None, "empty text"
        # JSON parse
        try:
            return json.loads(txt), None
        except json.JSONDecodeError:
            # Bazen markdown ```json ... ``` ile sarılı gelir
            m = re.search(r"\{.*\}", txt, re.DOTALL)
            if m:
                return json.loads(m.group(0)), None
            return None, f"json parse failed: {txt[:200]}"
    except Exception as e:
        return None, f"call error: {e}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def get_or_build_dossier(symbol: str, force_refresh: bool = False) -> dict:
    """Dossier üret veya cache'den döndür. Sonuç:
        {
          "symbol": ..., "symbol_type": ..., "payload": {...},
          "model_used": ..., "from_cache": bool, "created_at": iso, "error": Optional[str]
        }
    """
    sym = symbol.upper().strip()
    sym_type = _detect_type(sym)

    # 1) Cache lookup
    if not force_refresh:
        async with AsyncSessionLocal() as db:
            sql = text("""
                SELECT id, payload, model_used, created_at, error
                FROM trade_dossiers
                WHERE symbol = :sym AND created_at >= NOW() - INTERVAL ':ttl minutes'
                ORDER BY created_at DESC LIMIT 1
            """.replace(":ttl", str(CACHE_TTL_MIN)))
            res = await db.execute(sql, {"sym": sym})
            row = res.fetchone()
            if row:
                return {
                    "symbol": sym,
                    "symbol_type": sym_type,
                    "payload": row[1],
                    "model_used": row[2],
                    "from_cache": True,
                    "created_at": row[3].isoformat() if row[3] else None,
                    "error": row[4],
                }

    # 2) Veri topla
    logger.info(f"Dossier üretim başladı: {sym} ({sym_type})")
    t0 = time.time()

    bars = await asyncio.to_thread(_fetch_bars_for_type, sym, sym_type)
    ta = _ta_snapshot(bars) if bars else {"empty": True, "reason": "OHLC verisi yok"}
    fundamental = await _fundamental_block(sym, sym_type)
    news = await _news_block(sym)
    macro = await _macro_block(sym_type)

    # 3) Gemini sentez
    prompt = _DOSSIER_PROMPT_TEMPLATE.format(
        symbol=sym,
        symbol_type=sym_type,
        now_iso=datetime.now(timezone.utc).isoformat(),
        ta_block=json.dumps(ta, ensure_ascii=False, indent=2),
        fundamental_block=json.dumps(fundamental, ensure_ascii=False, indent=2),
        news_block=json.dumps(news, ensure_ascii=False, indent=2),
        macro_block=json.dumps(macro, ensure_ascii=False, indent=2),
    )

    parsed, err = await asyncio.to_thread(_call_gemini, prompt)
    payload = parsed or {
        "verdict": "TUT",
        "conviction": 1,
        "thesis": "Gemini üretim hatası — fallback",
        "key_drivers": [],
        "risks": [],
        "trigger": "Yenile butonuna tıklayın",
        "stop_level": "belirsiz",
        "target_levels": [],
        "horizon": "swing",
        "confidence_explainer": err or "üretim başarısız",
    }
    # Veri bloklarını payload'a ekle — UI "kaynak veri" sekmesinde gösterir
    payload["_data"] = {
        "ta": ta,
        "fundamental": fundamental,
        "news": news,
        "macro": macro,
    }

    # 4) DB'ye yaz
    async with AsyncSessionLocal() as db:
        d = TradeDossier(
            symbol=sym,
            symbol_type=sym_type,
            payload=payload,
            model_used=GEMINI_MODEL,
            error=err,
        )
        db.add(d)
        await db.commit()
        await db.refresh(d)
        created_at = d.created_at

    dt = time.time() - t0
    logger.info(f"Dossier üretildi: {sym} ({sym_type}) — {dt:.1f}s, verdict={payload.get('verdict')}, err={err}")

    return {
        "symbol": sym,
        "symbol_type": sym_type,
        "payload": payload,
        "model_used": GEMINI_MODEL,
        "from_cache": False,
        "created_at": created_at.isoformat() if created_at else None,
        "error": err,
    }
