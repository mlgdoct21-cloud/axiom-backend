"""Axiom Analistleri — Gemini ile snapshot'taki ham sayıları
Türkçe 6-bloklu karar çerçevesine çevirir (CryptoMe tarzı).

Pipeline:
1. get_onchain_snapshot(symbol) → 8-16 sinyal + axiom_score + breakdown
2. Snapshot'tan kompakt LLM context kur (sayıların kaynağı sadece bu)
3. Gemini 2.5-flash JSON → { headline, paragraphs[3], footer }
4. cryptoquant_cache (metric_key="story") 12h TTL ile sakla
5. Cache miss → yeniden üret

Tasarım:
- Sadece yatırım tavsiyesi DEĞİL, "ne oluyor" anlatımı.
- Sayılar yalnızca context'te geçenlerden kullanılır (LLM uydurmasın).
- Cache 12h: brifing günde 1 kez yenilenir, dashboard hızlı açılır.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import httpx

from core.logger import get_logger
from services.cryptoquant_service import (
    get_onchain_snapshot,
    _cache_get,
    _cache_set,
    _is_configured,
)
from services.etf_flow_cache_service import get_latest_etf_flow

logger = get_logger("crypto.storyteller")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_HTTP_TIMEOUT = httpx.Timeout(45.0, connect=8.0)
_CACHE_TTL = timedelta(hours=12)
_SUPPORTED = ("BTC", "ETH", "XRP")


# ── Snapshot → LLM context ────────────────────────────────────────────────

def _compact_signals(snapshot: dict) -> list[dict]:
    """Snapshot içindeki signals dict'ini sıkıştırılmış liste olarak döner.
    LLM kısa bir tablo görsün — her satırda metrik adı, değer, ne diyor."""
    out: list[dict] = []
    signals = snapshot.get("signals") or {}
    for key, s in signals.items():
        if not isinstance(s, dict):
            continue
        out.append({
            "metric": key,
            "value": s.get("value_str", ""),
            "label": s.get("label_tr", ""),
            "direction": s.get("signal", "NEUTRAL"),
        })
    return out


def _top_contributors(snapshot: dict, n: int = 3) -> dict:
    """En etkili 3 pozitif + 3 negatif signal — Gemini odağı belirlesin diye."""
    breakdown = snapshot.get("score_breakdown") or []
    pos = sorted(
        [b for b in breakdown if (b.get("contribution") or 0) > 0],
        key=lambda x: -(x.get("contribution") or 0),
    )[:n]
    neg = sorted(
        [b for b in breakdown if (b.get("contribution") or 0) < 0],
        key=lambda x: (x.get("contribution") or 0),
    )[:n]
    return {
        "supports": [
            {"metric": p["metric"], "label": p.get("label_tr", "")}
            for p in pos
        ],
        "pressures": [
            {"metric": n["metric"], "label": n.get("label_tr", "")}
            for n in neg
        ],
    }


def _direction_source(snapshot: dict) -> Optional[dict]:
    """Spot vs Türev yön kaynağı analizi.
    funding negatif + spot taker yüksek = spot-led (sağlıklı)
    funding pozitif + spot taker düşük = kaldıraç-led (kırılgan)
    """
    funding = snapshot.get("funding_rates")
    spot = snapshot.get("spot_taker")
    if not funding and not spot:
        return None
    f_val = funding.get("latest") if funding else None
    s_val = spot.get("ratio") if spot and spot.get("ratio") is not None else None
    return {
        "funding_rate": f_val,
        "spot_taker_ratio": s_val,
        "futures_open_interest": (
            snapshot.get("open_interest", {}).get("change_pct")
            if snapshot.get("open_interest") else None
        ),
    }


async def _etf_block(symbol: str) -> Optional[dict]:
    """BTC + ETH için ETF flow özeti — spot kurumsal talep göstergesi."""
    if symbol not in ("BTC", "ETH"):
        return None
    try:
        flow = await get_latest_etf_flow(symbol)
    except Exception as e:
        logger.warning(f"etf flow read failed for {symbol}: {e}")
        return None
    if not flow:
        return None
    return {
        "net_flow_usd": flow.get("net_flow_usd"),
        "net_flow_coins": flow.get("net_flow_coins"),
        "scraped_at": flow.get("scraped_at"),
        "is_fresh": flow.get("is_fresh"),
        "age_hours": flow.get("age_hours"),
    }


async def _build_context(snapshot: dict) -> dict:
    sym = snapshot.get("symbol")
    return {
        "symbol": sym,
        "axiom_score": snapshot.get("axiom_score"),
        "score_zone": snapshot.get("score_zone_tr"),
        "score_summary": snapshot.get("score_summary"),
        "overall": snapshot.get("overall_tr"),
        "signals": _compact_signals(snapshot),
        "drivers": _top_contributors(snapshot),
        "direction_source": _direction_source(snapshot),
        "etf_flow": await _etf_block(sym) if sym else None,
        "fetched_at": snapshot.get("fetched_at"),
    }


# ── Prompt ─────────────────────────────────────────────────────────────────

_SYMBOL_PERSONALITY = {
    "BTC": (
        "BTC dilinde 'döngü', 'kurumsal akış', 'spot ETF', 'madenci', 'kohort' "
        "(STH/LTH) anahtar kavramlardır. Bitcoin'i bir 'rezerv varlık' olarak "
        "konumlandır; perakende heyecanı yerine uzun-soluklu sermaye davranışına "
        "odaklan."
    ),
    "ETH": (
        "ETH dilinde 'staking', 'gas/aktif kullanım', 'L2', 'spot ETH ETF', "
        "'beacon chain çıkışları', 'borsa arz oranı' anahtar kavramlardır. "
        "Ethereum'u 'üretken bir altyapı varlığı' olarak konumlandır; ağ kullanım "
        "ve doğrulayıcı davranışı vurgu ön planda."
    ),
    "XRP": (
        "XRP dilinde 'türev tarafı dominasyonu', 'likidasyon kaskadı', 'düşük "
        "doğrulayıcı maliyeti', 'tx hacmi', 'tek noktalı whale dağılımı' anahtar "
        "kavramlardır. XRP'yi 'yüksek-volatil türev oyun alanı' olarak konumlandır; "
        "spot ETF veya madenci kavramı KULLANMA — XRP PoS değil, PoW de değil, "
        "RPCA konsensüsü ile çalışır."
    ),
}


def _build_prompt(ctx: dict) -> str:
    sym = ctx.get("symbol", "?")
    personality = _SYMBOL_PERSONALITY.get(sym, "")
    has_etf = bool(ctx.get("etf_flow"))
    return (
        f"Sen 'Axiom Analistleri' takımısın — {sym} için on-chain veriyi "
        "Türk kripto yatırımcısına KARAR ÇERÇEVESİ olarak sunuyorsun. "
        "CryptoMe akademi yazarının üslubunu örnek al: numaralı engel/aşama "
        "dizimi, 'eğer X olursa Y' koşullu cümleler, kendi pozisyonunu "
        "açıkça ilan etme + revizyon koşulu, sıcak ama kurumsal Türkçe.\n\n"
        f"### {sym} kişiliği:\n{personality}\n\n"
        f"### INPUT JSON (TÜM sayılar buradan; başka kaynak YASAK):\n"
        f"{json.dumps(ctx, ensure_ascii=False)}\n\n"
        "### ÇIKTI ŞEMASI (sadece JSON):\n"
        "{\n"
        '  "headline": string,        // max 120 karakter; başlık-soru veya net iddia\n'
        '  "paragraphs": [string × 6],\n'
        '  "footer": string\n'
        "}\n\n"
        "### 6 BLOK (sırayla, her biri 2-4 cümle):\n"
        "1) BAŞLIK SORUSU + NET CEVAP\n"
        "   Headline'daki soruyu/iddiayı NET cevapla. Axiom skoru + bölge "
        "   (Güvenli/Dikkatli/Riskli/Fırsat) burada geçsin.\n"
        "2) AŞILAN EŞİKLER (✅)\n"
        "   drivers.supports'tan EN AZ 2 sinyali 'engelleri geçtik' "
        "   çerçevesinde anlat. SOPR Ratio varsa ve 1.05'in altındaysa 'kohortlar "
        "   dengede, taze para hareketleniyor' nuansını ver; 1.15'in üstündeyse "
        "   'uzun vadeci dağıtım baskın — tepe uyarı bölgesi' diye uyar.\n"
        "3) HENÜZ AŞILMAYAN EŞİKLER (⏳)\n"
        "   drivers.pressures veya NEUTRAL'lardan 1-2 madde. 'Şu seviyeye "
        "   gelirse şu anlama gelir' formatı şart. Korean Premium negatifse "
        "   'Asya tarafı henüz teyit etmedi — Upbit primi sıfırı geçince ralli "
        "   onayı tamamlanır' gibi bir koşul ekleyebilirsin.\n"
        "4) YÖN KAYNAĞI: SPOT MU TÜREV Mİ? (yeni)\n"
        "   direction_source bloğunu yorumla:\n"
        "   • funding NEGATIF + spot_taker_ratio >1.0 = SPOT-led rally "
        "     (sağlıklı, kurumsal alıcı baskın)\n"
        "   • funding POZİTİF + spot_taker düşük = KALDIRAÇ-led "
        "     (kırılgan, short squeeze tehdidi)\n"
        "   • btc_liquidations sinyali varsa: long tasfiyesi baskın = forced "
        "     sell biteliyor (dip onayı), short sıkışması baskın = ralli "
        "     yapay olabilir; çift yönlü tasfiye = türev tarafı temizleniyor.\n"
        + (
            "   • etf_flow.net_flow_usd POZİTİF (>50M) = spot ETF kurumsal "
            "talebi destekliyor; NEGATİF = kurumsal çıkış. ETF rakamını "
            "açıkça yaz (örn '$67M giriş' veya '$112M çıkış').\n"
            if has_etf else ""
        ) +
        "   Bu blok yoksa çıkarma — 'yön kaynağı net okunmuyor' diye yaz.\n"
        "5) TETİKLEYİCİ + REVİZYON KOŞULU\n"
        "   İki yönlü: (a) 'Eğer [metrik] [eşik]'i geçerse Axiom'un kanaati "
        "   [şu yöne] döner', (b) 'Aşağıda [metrik] [eşik]'in altına düşerse "
        "   erken uyarı veririz'. EN AZ 1 yukarı + 1 aşağı koşul.\n"
        "6) AXIOM'UN POZİSYONU + İZLENECEK TEK METRİK\n"
        "   'Şu an Axiom skoru X — [bölge]. Yukarı doğru [pratik anlam], "
        "   aşağı [pratik anlam]. Önümüzdeki günlerde özellikle [TEK metrik] "
        "   izleyin.'\n\n"
        "### DİL ÇEŞİTLİLİĞİ — KESİN KURAL:\n"
        "Şu kalıp başlıkları KULLANMA (her sembolde tekrar ediyor, kötü):\n"
        "  ✗ 'X için boğa momentumu geri mi dönüyor?'\n"
        "  ✗ 'Karışık sinyallerle dikkatli bir dönem'\n"
        "  ✗ 'X için yükseliş rüzgarı esiyor mu?'\n"
        "Bunun yerine sembolün KENDİ kişiliğine özgü başlık ürün. Örn:\n"
        "  ✓ BTC: 'Spot ETF girişi mi, kaldıraçlı sıçrama mı? STH-SOPR cevap veriyor.'\n"
        "  ✓ ETH: 'Borsa arzı düşüyor, doğrulayıcı sırası uzuyor — ama funding henüz uyumadı.'\n"
        "  ✓ XRP: 'Türev tarafı çift yönlü temizleniyor; spot agresörler hâlâ kararsız.'\n\n"
        "### JARGON SÖZLÜĞÜ (mutlaka çevir):\n"
        "- netflow → 'borsa akışı (giren-çıkan fark)'\n"
        "- funding rate → 'fonlama oranı — kaldıraçlı pozisyonların yön ücreti'\n"
        "- whale ratio → 'balina oranı'\n"
        "- MVRV → 'gerçekleşmiş kâr/zarar oranı'\n"
        "- SOPR → 'satılan paraların kâr katsayısı'\n"
        "- SOPR Ratio → 'uzun/kısa vadeci kâr dengesi'\n"
        "- liquidations → 'tasfiye (zorla kapatılan kaldıraçlı pozisyonlar)'\n"
        "- Korean Premium → 'Upbit primi (Asya retail iştahı)'\n"
        "- MPI → 'madenci satış baskısı endeksi'\n"
        "- spot taker ratio → 'spot alıcı/satıcı oranı'\n"
        "- open interest → 'açık pozisyon hacmi'\n"
        "- ETF flow → 'spot ETF net girişi/çıkışı'\n\n"
        "### KESİN YASAKLAR:\n"
        "- Sayı UYDURMA — INPUT'taki signals[].value veya axiom_score "
        "  veya direction_source veya etf_flow alanlarından gelmeyen sayı YOK.\n"
        "- JSON LİTERAL KOPYALAMA — INPUT'taki { \"key\": value } parçalarını "
        "  asla aynen yazma. Sayıları düz Türkçe metin içinde göster. ÖRN:\n"
        "    ✗ 'Balina oranı { \"value\": \"0.56\" } ile normal seviyelerde'\n"
        "    ✓ 'Balina oranı 0,56 ile normal seviyelerde' (veya '%56' uygunsa)\n"
        "    ✗ '{ \"axiom_score\": 72.3 } puan'\n"
        "    ✓ '72,3 puan' veya 'Axiom skoru 72'\n"
        "  Türkçe yazıda ondalık ayraç virgül (0,56), binlik nokta (1.000).\n"
        "- 'Al', 'sat', 'tut', 'pozisyon aç', 'hedef fiyat', 'stop koy', "
        "  'long aç', 'short aç' YASAK. Yerine 'izleyin', 'fikrimiz değişir', "
        "  'erken uyarı veririz' kullan.\n"
        "- Emoji veya markdown başlığı kullanma. Sadece (✅), (⏳) inline "
        "  durum işaretleri paragraf içinde 1-2 kez kullanılabilir.\n"
        "- 'belki', 'olabilir' tahmin dilini SINIRLA — koşullu 'eğer-ise' tercih et.\n"
        "- 'dostlar', 'arkadaşlar' hitap YASAK. Kullanıcıyı 'siz' diye çağır.\n"
        "- Diğer sembollerin kişiliğini KARIŞTIRMA: BTC'de XRP terimi yok, "
        "  XRP'de madenci terimi yok.\n"
        "- footer her zaman: 'Bu analiz on-chain veriyi yorumlar; pozisyon "
        "  kararı sizindir. Yatırım tavsiyesi değildir.'\n"
    )


# ── Gemini call ───────────────────────────────────────────────────────────

async def _call_gemini(prompt: str) -> Optional[dict]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or "buraya" in api_key:
        logger.error("GEMINI_API_KEY missing for storyteller")
        return None
    url = GEMINI_URL.format(model=GEMINI_MODEL, key=api_key)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.55,
            # 4500 → 8000 → 12000: 6 Türkçe paragraf JSON-encode edilince
            # 3000+ char çıkıyor, lower limitler mid-string truncate ediyordu.
            "maxOutputTokens": 12000,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            logger.warning(f"storyteller gemini {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"storyteller gemini call failed: {e}")
        return None

    candidates = data.get("candidates") or []
    if not candidates:
        return None
    raw = (
        candidates[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
        .strip()
    )
    if not raw:
        return None
    fence = re.search(r"\{[\s\S]*\}", raw)
    payload = fence.group(0) if fence else raw
    try:
        return json.loads(payload)
    except Exception as e:
        logger.warning(f"storyteller json parse failed: {e}; raw={raw[:200]}")
        return None


# ── Halüsinasyon önleyici: numeric validator ──────────────────────────────

# Genel kabul gören metrik eşikleri ve sentinel değerler.
# Bunlar prompt'ta açıkça eşik olarak geçtiği için (örn 'SOPR 1.0', 'whale 0.85')
# whitelist'e baştan ekleniyor.
_THRESHOLD_SENTINELS = {
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
    "12", "15", "20", "24", "25", "30", "31", "50", "51", "60", "70", "71",
    "86", "100", "155", "365",
    "0.5", "0.7", "0.85", "0.9", "0.95", "0.97", "0.98",
    "1.0", "1.02", "1.03", "1.05", "1.5", "2.0", "3.0", "3.7", "4.0",
    "0.10", "0.13", "0.18", "0.25", "0.50", "0.75",
}

_NUM_RE = re.compile(r"[-+]?\d{1,3}(?:[,.]\d{3})*(?:[.,]\d+)?")


def _normalize_num(s: str) -> Optional[float]:
    """'+989.000.000' → 989000000.0  ·  '$67M' → 67  ·  '%-5.2' → 5.2"""
    s = s.strip().lstrip("+").lstrip("$").rstrip("%").rstrip("M").rstrip("B").rstrip("K")
    if not s or s in {"-", "+", "."}:
        return None
    try:
        # '989.000.000' Avrupa formatı? Birden fazla nokta varsa ayraç kabul et
        if s.count(".") > 1:
            s = s.replace(".", "")
        # Karışık virgül-nokta: en sağdaki ondalık say
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            # Tek virgül → ondalık ayraç (TR formatı) ya da binlik (EN)
            # Eğer 1-3 hane varsa ondalık say
            after = s.split(",")[-1]
            if len(after) <= 2:
                s = s.replace(",", ".")
            else:
                s = s.replace(",", "")
        return float(s)
    except (ValueError, AttributeError):
        return None


def _collect_allowed_numbers(ctx: dict) -> set[float]:
    """Snapshot context'inden TÜM sayıları topla — uydurma kontrolü için
    whitelist. Eşik sentinel'leri + signals[].value içindeki tüm sayılar +
    direction_source + etf_flow + axiom_score."""
    allowed: set[float] = set()
    for s in _THRESHOLD_SENTINELS:
        v = _normalize_num(s)
        if v is not None:
            allowed.add(v)

    score = ctx.get("axiom_score")
    if score is not None:
        try:
            allowed.add(float(score))
        except (TypeError, ValueError):
            pass

    for sig in ctx.get("signals", []) or []:
        v_str = sig.get("value", "") or ""
        for m in _NUM_RE.findall(v_str):
            n = _normalize_num(m)
            if n is None:
                continue
            allowed.add(n)
            allowed.add(abs(n))
            # ölçek varyantları: M=milyon, B=milyar, K=bin → ham sayı
            if "M" in v_str.upper():
                allowed.add(n * 1_000_000)
            if "B" in v_str.upper():
                allowed.add(n * 1_000_000_000)
            if "K" in v_str.upper():
                allowed.add(n * 1_000)

    ds = ctx.get("direction_source") or {}
    for k in ("funding_rate", "spot_taker_ratio", "futures_open_interest"):
        v = ds.get(k)
        if v is not None:
            try:
                allowed.add(float(v))
                allowed.add(float(v) * 100)
            except (TypeError, ValueError):
                pass

    ef = ctx.get("etf_flow") or {}
    for k in ("net_flow_usd", "net_flow_coins", "age_hours"):
        v = ef.get(k)
        if v is not None:
            try:
                f = float(v)
                allowed.add(f)
                allowed.add(abs(f))
                # USD → milyon ölçek (LLM '$478M' yazabilir)
                if k == "net_flow_usd":
                    allowed.add(f / 1_000_000)
                    allowed.add(abs(f / 1_000_000))
            except (TypeError, ValueError):
                pass

    return allowed


def _find_hallucinated_numbers(
    text: str, allowed: set[float], tol_rel: float = 0.02
) -> list[float]:
    """text'teki sayılardan whitelist'e (±%2 tolerans) düşmeyenleri döner.
    Telemetri amaçlı; bu liste boş değilse Gemini'yi 1 kez yeniden çağırırız."""
    found: set[float] = set()
    for raw in _NUM_RE.findall(text):
        n = _normalize_num(raw)
        if n is None or n == 0:
            continue
        # Tek hane (1-9) sayılar genelde sıralama/sayma — affet
        if abs(n) < 10 and n == int(n):
            continue
        found.add(n)

    bad: list[float] = []
    for n in found:
        ok = False
        for a in allowed:
            if a == 0:
                if n == 0:
                    ok = True
                    break
                continue
            if abs(n - a) <= tol_rel * max(abs(a), abs(n)):
                ok = True
                break
        if not ok:
            bad.append(n)
    return sorted(bad)


def _validate_against_context(
    out: dict, ctx: dict, *, max_bad: int = 2
) -> tuple[Optional[dict], list[float]]:
    """Önce şema validate, sonra numeric. max_bad'dan fazla uydurma sayı
    varsa None döner (caller retry yapsın). Returns (parsed, hallucinated_list)."""
    parsed = _validate(out)
    if not parsed:
        return None, []
    full_text = " ".join([parsed["headline"], *parsed["paragraphs"], parsed["footer"]])
    allowed = _collect_allowed_numbers(ctx)
    bad = _find_hallucinated_numbers(full_text, allowed)
    if len(bad) > max_bad:
        return None, bad
    return parsed, bad


def _validate(out: dict) -> Optional[dict]:
    if not isinstance(out, dict):
        return None
    headline = (out.get("headline") or "").strip()
    paragraphs = out.get("paragraphs") or []
    footer = (out.get("footer") or "").strip()
    if not headline or not isinstance(paragraphs, list) or len(paragraphs) < 2:
        return None
    paragraphs = [p.strip() for p in paragraphs if isinstance(p, str) and p.strip()]
    # 6 blok hedef; 4'ten azsa rejekt — model en azından
    # cevap+aşılan+kalan+revizyon dörtlüsünü vermeli.
    if len(paragraphs) < 4:
        return None
    if not footer:
        footer = "Bu analiz on-chain veriyi yorumlar; pozisyon kararı sizindir. Yatırım tavsiyesi değildir."
    return {
        "headline": headline[:180],
        "paragraphs": paragraphs[:6],
        "footer": footer[:240],
    }


# ── Public API ────────────────────────────────────────────────────────────

async def get_onchain_story(symbol: str = "BTC", *, force: bool = False) -> dict:
    """Public entry — cache veya yeniden üret. 503 dönmez; envelope ile error verir."""
    sym = (symbol or "BTC").upper().strip()
    if sym not in _SUPPORTED:
        return {
            "error": "symbol_not_supported",
            "symbol": sym,
            "supported": list(_SUPPORTED),
        }
    if not _is_configured():
        return {"error": "cryptoquant_not_configured", "symbol": sym}

    if not force:
        cached = await _cache_get("story", sym, "day")
        if cached and cached.get("headline"):
            return cached

    snapshot = await get_onchain_snapshot(sym)
    if not snapshot or snapshot.get("error"):
        return {"error": "snapshot_unavailable", "symbol": sym}

    ctx = await _build_context(snapshot)
    if not ctx.get("axiom_score") and not ctx.get("signals"):
        return {"error": "no_signals", "symbol": sym}

    # 1. tur — normal prompt
    prompt = _build_prompt(ctx)
    raw_out = await _call_gemini(prompt)
    parsed, bad = _validate_against_context(raw_out or {}, ctx)

    # 2. tur — uydurma sayı varsa Gemini'ye geri besleme + sıkıştırılmış yeniden dene
    if not parsed and bad:
        logger.warning(
            f"storyteller {sym}: hallucinated numbers detected: {bad[:5]} — retrying"
        )
        retry_prompt = (
            prompt
            + "\n\n### KRİTİK DÜZELTME (önceki cevabınız reddedildi):\n"
            + f"Şu sayılar INPUT JSON'da YOK ve uydurma kabul edildi: {bad[:8]}\n"
            + "Yeniden üret. Sadece INPUT'taki signals[].value, axiom_score, "
            + "direction_source, etf_flow alanlarındaki sayıları kullan. "
            + "Bilmediğin bir sayıyı yazmak yerine sayısız anlatım yap."
        )
        raw_out2 = await _call_gemini(retry_prompt)
        parsed, bad = _validate_against_context(raw_out2 or {}, ctx, max_bad=4)
        # 2. tur biraz daha gevşek (max_bad=4): kullanıcıya hiç hikaye
        # vermemektense ufak şüpheli sayılı bir hikaye iyi.

    if not parsed:
        # 1. tur şema-fail veya 2 tur halüsinasyon — yine de düşmemek için
        # ham çıktıdan şema-only cevap kabul et
        parsed = _validate(raw_out or {})
    if not parsed:
        return {"error": "story_generation_failed", "symbol": sym}

    payload = {
        **parsed,
        "symbol": sym,
        "axiom_score": ctx.get("axiom_score"),
        "score_zone": ctx.get("score_zone"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "_validator": {
            "hallucinated_count": len(bad),
            "samples": [str(n) for n in bad[:5]],
        },
    }
    await _cache_set("story", sym, "day", payload, _CACHE_TTL)
    return payload


async def refresh_story(symbol: str) -> dict:
    return await get_onchain_story(symbol, force=True)
