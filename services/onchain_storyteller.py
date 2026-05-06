"""On-Chain Storyteller — Gemini ile snapshot'taki ham sayıları
Türkçe 3-paragraflık bir hikâyeye çevirir.

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

logger = get_logger("crypto.storyteller")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_HTTP_TIMEOUT = httpx.Timeout(25.0, connect=8.0)
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


def _build_context(snapshot: dict) -> dict:
    return {
        "symbol": snapshot.get("symbol"),
        "axiom_score": snapshot.get("axiom_score"),
        "score_zone": snapshot.get("score_zone_tr"),
        "score_summary": snapshot.get("score_summary"),
        "overall": snapshot.get("overall_tr"),
        "signals": _compact_signals(snapshot),
        "drivers": _top_contributors(snapshot),
        "fetched_at": snapshot.get("fetched_at"),
    }


# ── Prompt ─────────────────────────────────────────────────────────────────

def _build_prompt(ctx: dict) -> str:
    sym = ctx.get("symbol", "?")
    return (
        f"Sen AXIOM'un on-chain hikâyeleştirici mentörüsün. {sym} için ham "
        "CryptoQuant sinyallerini Türk kripto yatırımcısının anlayacağı, "
        "CryptoMe tarzı bir KARAR ÇERÇEVESİNE çevir. Sayı dökmek değil, "
        "okuyucuya 'hangi engelleri geçtik, hangileri kaldı, ne olursa fikir değişir' "
        "diye yön göster.\n\n"
        f"INPUT JSON:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
        "ÇIKTI ŞEMASI (sadece JSON, başka hiçbir şey yok):\n"
        "{\n"
        '  "headline": string,        // 1 cümle, max 110 karakter, başlık-soru tercih edilir\n'
        '                              // örn: "BTC için boğa döndü mü? — Erken sinyaller var, kapılar açılmadı."\n'
        '  "paragraphs": [string, string, string, string, string],\n'
        '  "footer": string           // 1 cümle uyarı\n'
        "}\n\n"
        "PARAGRAF YAPISI (5 BLOK — sırayla, her biri 2-4 cümle):\n"
        "1) BAŞLIK SORUSU + NET CEVAP\n"
        "   Headline'daki soruyu açıkça cevapla. 'Evet ama henüz değil', "
        "   'Hayır, şu sebeple', 'Evet ve şu engel de düştü' gibi NET pozisyon al. "
        "   Axiom skoru + bölge (Güvenli/Dikkatli/Riskli/Fırsat) burada geçsin.\n"
        "2) AŞILAN EŞİKLER (✅ engelleri geçtik)\n"
        "   drivers.supports listesinden EN AZ 2 sinyali 'engelleri geçtik' "
        "   çerçevesinde anlat. Her metrik için: DEĞER + NE ANLAMA GELDİĞİ + "
        "   neden bu seviye olumlu.\n"
        "3) HENÜZ AŞILMAYAN EŞİKLER (⏳ kalan engeller)\n"
        "   drivers.pressures veya NEUTRAL signals'tan EN AZ 1-2 maddeyi "
        "   'bekleyen engel' olarak göster. 'Şu seviyeye gelirse şu anlama gelir' "
        "   formatı şart. Örn: 'Funding +0.01'in altında kalmaya devam ederse "
        "   kaldıraçlı boğa iştahı henüz uyanmamış demektir.'\n"
        "4) TETİKLEYİCİ VE REVİZYON KOŞULU\n"
        "   İki yönlü: (a) 'Eğer [metrik] [eşik]'i geçerse fikrimiz [şu yöne] döner' "
        "   ve (b) 'Aşağıda [metrik] [eşik]'in altına düşerse erken uyarı veririz: ...'. "
        "   En az BİR yukarı koşul + BİR aşağı koşul olsun.\n"
        "5) AXIOM'UN POZİSYONU + İZLENECEK TEK METRİK\n"
        "   'Şu an Axiom skoru X — [bölge]. Yukarı doğru ilerlerse [pratik anlam], "
        "   aşağı kayarsa [pratik anlam]. Önümüzdeki günlerde özellikle "
        "   [tek bir metrik adı] gözlenmeli.' Tek metrik seç — odağı dağıtma.\n\n"
        "KURALLAR (kesin):\n"
        "- Türkçe; finansal jargonu çevir: 'netflow'='borsa akışı (giren-çıkan fark)', "
        "  'funding rate'='fonlama oranı — kaldıraçlı pozisyonların yön ücreti', "
        "  'whale ratio'='balina oranı — büyük adreslerin payı', "
        "  'MVRV'='gerçekleşmiş kâr/zarar oranı', 'SOPR'='satılan paraların kâr katsayısı', "
        "  'MPI'='madenci satış baskısı endeksi', 'open interest'='açık pozisyon hacmi'.\n"
        "- HER sayı INPUT'taki signals[].value veya axiom_score'dan gelmeli; "
        "  başka sayı UYDURMA. Eşik (1.0, 0.85, 0 gibi) genel kabul gören metrik "
        "  eşiği ise yazabilirsin ama snapshot'taki gerçek değerle birlikte ver.\n"
        "- 'Al', 'sat', 'tut', 'pozisyon aç', 'hedef fiyat', 'stop koy' YASAK. "
        "  Bunun yerine 'şu eşiği izleyin', 'şu seviyenin altına düşerse uyarı', "
        "  'Axiom'un kanaati şu yöne döner' kullan.\n"
        "- Emoji veya markdown başlığı KULLANMA. Sadece (✅), (⏳) gibi inline "
        "  durum işaretlerini paragraf İÇİNDE doğal kullanabilirsin (her blokta 1-2 tane).\n"
        "- 'belki', 'olabilir' tahmin dilini SINIRLA; verinin söylediğini söyle, "
        "  'eğer-ise' koşullu cümleleri tercih et.\n"
        "- Mentör tonu: 'dostlar', 'arkadaşlar' gibi hitap KULLANMA — kurumsal "
        "  ama sıcak Türkçe. Kullanıcıyı 'siz' diye çağır.\n"
        "- footer her zaman: 'Bu analiz on-chain veriyi yorumlar; pozisyon kararı "
        "  sizindir. Yatırım tavsiyesi değildir.' veya birebir benzeri.\n"
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
            "temperature": 0.4,
            "maxOutputTokens": 3500,
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


def _validate(out: dict) -> Optional[dict]:
    if not isinstance(out, dict):
        return None
    headline = (out.get("headline") or "").strip()
    paragraphs = out.get("paragraphs") or []
    footer = (out.get("footer") or "").strip()
    if not headline or not isinstance(paragraphs, list) or len(paragraphs) < 2:
        return None
    paragraphs = [p.strip() for p in paragraphs if isinstance(p, str) and p.strip()]
    # 5 blok hedef, ama 3'ten azsa rejekt — model en azından
    # cevap+aşılan+kalan üçlüsünü vermeli.
    if len(paragraphs) < 3:
        return None
    if not footer:
        footer = "Bu analiz on-chain veriyi yorumlar; pozisyon kararı sizindir. Yatırım tavsiyesi değildir."
    return {
        "headline": headline[:160],
        "paragraphs": paragraphs[:5],
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

    ctx = _build_context(snapshot)
    if not ctx.get("axiom_score") and not ctx.get("signals"):
        return {"error": "no_signals", "symbol": sym}

    raw_out = await _call_gemini(_build_prompt(ctx))
    parsed = _validate(raw_out) if raw_out else None
    if not parsed:
        return {"error": "story_generation_failed", "symbol": sym}

    payload = {
        **parsed,
        "symbol": sym,
        "axiom_score": ctx.get("axiom_score"),
        "score_zone": ctx.get("score_zone"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set("story", sym, "day", payload, _CACHE_TTL)
    return payload


async def refresh_story(symbol: str) -> dict:
    return await get_onchain_story(symbol, force=True)
