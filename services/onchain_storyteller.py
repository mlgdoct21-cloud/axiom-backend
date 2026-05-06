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
        f"Sen AXIOM'un on-chain hikâyeleştirici ajanısın. {sym} için ham "
        "CryptoQuant sinyallerini Türk perakende kripto yatırımcısının "
        "anlayacağı sade bir hikâyeye çevir.\n\n"
        f"INPUT JSON:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
        "ÇIKTI ŞEMASI (sadece JSON, başka hiçbir şey yok):\n"
        "{\n"
        '  "headline": string,        // 1 cümle, max 90 karakter, çarpıcı ama abartısız\n'
        '  "paragraphs": [string, string, string],\n'
        '  "footer": string           // 1 cümle uyarı\n'
        "}\n\n"
        "PARAGRAF YAPISI:\n"
        "1) GENEL DURUM: Axiom skoru + ne anlama geldiği (Güvenli/Dikkatli/Riskli/Tehlikeli/Fırsat) "
        "+ overall sinyal — 2-3 cümle.\n"
        "2) NEDEN: drivers.supports ve drivers.pressures listesindeki metriklerden "
        "en az 2'sini somut sayılarla anlatarak skoru NE besliyor NE baskılıyor — 3-4 cümle.\n"
        "3) NE İZLENMELİ: signals listesindeki BEARISH veya NEUTRAL durumdaki bir-iki metriğe "
        "işaret edip 'şu eşiği geçerse veya şu seviyeye gelirse durum değişir' tarzı "
        "ileriye dönük bir gözlem — 2-3 cümle.\n\n"
        "KURALLAR:\n"
        "- Türkçe yaz; finansal jargonu çevir (örn. 'netflow' yerine 'borsa akışı', "
        "'funding rate' yerine 'fonlama oranı: kaldıraçlı pozisyonların yönü').\n"
        "- HER sayı INPUT'taki signals[].value veya axiom_score değerlerinden gelmeli; uydurma.\n"
        "- 'Al', 'sat', 'tut', 'pozisyon aç', 'hedef fiyat' gibi tavsiye dili YASAK.\n"
        "- Emoji veya markdown başlığı KULLANMA; düz metin paragraflar.\n"
        "- footer her zaman: 'Yatırım tavsiyesi değildir; on-chain veriler tek başına karar mercii olamaz.' "
        "veya benzeri 1 cümlelik uyarı.\n"
        "- 'belki', 'olabilir' tarzı tahmin dilini sınırla; verinin SÖYLEDİĞİNİ söyle.\n"
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
            "maxOutputTokens": 2048,
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
    if len(paragraphs) < 2:
        return None
    if not footer:
        footer = "Yatırım tavsiyesi değildir; on-chain veriler tek başına karar mercii olamaz."
    return {
        "headline": headline[:140],
        "paragraphs": paragraphs[:3],
        "footer": footer[:200],
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
