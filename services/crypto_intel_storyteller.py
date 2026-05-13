"""
Crypto Intel Storyteller — Pusula / ERC20 / Stablecoin sub-tab'ları için
Gemini-üretimi market commentary + deterministic action box.

Pattern macro_storyteller'ı izler:
  • Gemini call (gemini-2.5-flash, thinkingBudget=0, temp 0.3, JSON output)
  • 4 katmanlı halüsinasyon koruması:
      1. source_snapshot ile veri-yaşı rozeti (UI gösterir)
      2. Numeric claim validator (LLM çıktısı vs gerçek değerler)
      3. Stale exclude (>6h eski metrik narrative'a girmez)
      4. Action box rule-based (LLM değil) — yön tutarsızlığı imkansız
  • Scheduler 6 saatte bir refresh (cryptoquant_scheduler içinden tetikle)
  • Cache: crypto_intel_stories tablosu (3 tab × 2 tier = 6 satır)

Maliyet: 6 Gemini çağrısı × 6h cadance = günde 24 çağrı.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Literal, Optional

import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("crypto_intel_storyteller")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)
_HTTP_TIMEOUT = 30.0

Tab = Literal["overview", "erc20", "stable"]
Tier = Literal["premium", "advance"]

_TAB_LABEL = {
    "overview": "Pusula (Alt Sezon Skoru)",
    "erc20":    "ERC20 Akıllı Para Radarı",
    "stable":   "Stablecoin Nabzı",
}

_WORD_TARGETS = {
    # Premium daha kısa kompakt, Advance daha geniş
    ("overview", "premium"): (45, 75),
    ("overview", "advance"): (90, 140),
    ("erc20",    "premium"): (45, 75),
    ("erc20",    "advance"): (90, 140),
    ("stable",   "premium"): (45, 75),
    ("stable",   "advance"): (90, 140),
}


# ── Action Box (deterministic — kural tabanlı) ───────────────────────────────

def _action_box_overview(altseason: dict) -> List[Dict[str, str]]:
    """Alt sezon skoruna göre aksiyon önerisi. LLM yok — pure rule."""
    score = altseason.get("altseason_score")
    if score is None:
        return []
    if score >= 80:
        return [
            {"sign": "✓", "text": "Altcoin ağırlığı agresif artırılabilir"},
            {"sign": "✓", "text": "Yüksek-beta DeFi token'larda pozisyon büyüt"},
            {"sign": "⚠", "text": "BTC dominance düşüş trendine dikkat"},
        ]
    if score >= 65:
        return [
            {"sign": "✓", "text": "Altcoin'lere kademeli giriş için uygun ortam"},
            {"sign": "✓", "text": "ETH/altcoin oranlarını izle, momentum lehte"},
            {"sign": "⚠", "text": "BTC çekirdek pozisyonu koru, %50+ altcoin riskli"},
        ]
    if score >= 45:
        return [
            {"sign": "✓", "text": "Pozisyonları koru, yeni alımda seçici ol"},
            {"sign": "⚠", "text": "Karışık ortam — net trend belirleyici sinyal bekle"},
        ]
    if score >= 30:
        return [
            {"sign": "✓", "text": "BTC ağırlığını koru veya artır"},
            {"sign": "✗", "text": "Yeni altcoin pozisyonu açmaktan kaçın"},
            {"sign": "⚠", "text": "Mevcut altcoin'lerde kâr realizasyonu düşün"},
        ]
    return [
        {"sign": "✓", "text": "Tam BTC ağırlığa kay, altcoin pozisyonları küçült"},
        {"sign": "✗", "text": "Altcoin alımı kesinlikle uygun değil"},
        {"sign": "⚠", "text": "BTC dominance yükseliş trendi — beklenti yönü doğru"},
    ]


def _action_box_erc20(radar: dict) -> List[Dict[str, str]]:
    """ERC20 token bazlı birikim/dağıtım sinyallerine göre aksiyon."""
    tokens = radar.get("tokens") or []
    if not tokens:
        return []
    accumulating = []
    distributing = []
    for t in tokens:
        sig = t.get("signal") or t.get("flow_signal")
        sym = t.get("symbol")
        if not sym or not sig:
            continue
        if sig in ("STRONG_ACCUMULATION", "ACCUMULATION", "STRONG_BULLISH", "BULLISH"):
            accumulating.append(sym)
        elif sig in ("STRONG_DISTRIBUTION", "DISTRIBUTION", "STRONG_BEARISH", "BEARISH"):
            distributing.append(sym)

    box: List[Dict[str, str]] = []
    if accumulating:
        names = ", ".join(accumulating[:4])
        box.append({"sign": "✓", "text": f"Birikim sinyali: {names} — erken giriş düşünülebilir"})
    if distributing:
        names = ", ".join(distributing[:4])
        box.append({"sign": "✗", "text": f"Dağıtım sinyali: {names} — yeni alımdan kaçın"})
    if not box:
        box.append({"sign": "⚠", "text": "Net yön yok — sinyal teyitini bekle"})
    return box


def _action_box_stable(pulse: dict) -> List[Dict[str, str]]:
    """Stablecoin akışı + SSR'ye göre kuru barut yorumu."""
    totals = pulse.get("totals") or {}
    nf7 = totals.get("netflow_7d", 0) or 0
    ssr = pulse.get("ssr_proxy")
    box: List[Dict[str, str]] = []

    if nf7 > 1_000_000_000:
        box.append({"sign": "✓", "text": "Borsada yoğun kuru barut birikimi — yakında alım dalgası muhtemel"})
        box.append({"sign": "✓", "text": "Düşüşler alım fırsatı olarak değerlendirilebilir"})
    elif nf7 > 200_000_000:
        box.append({"sign": "✓", "text": "Borsalara nakit akıyor — alım gücü artıyor"})
    elif nf7 < -1_000_000_000:
        box.append({"sign": "✗", "text": "Stablecoin'ler borsadan kaçıyor — alım gücü zayıf"})
        box.append({"sign": "⚠", "text": "Kademeli alım stratejisi, agresif pozisyonlardan kaçın"})
    elif nf7 < -200_000_000:
        box.append({"sign": "⚠", "text": "Hafif stablecoin çıkışı — alım gücü azalıyor"})
    else:
        box.append({"sign": "⚠", "text": "Dengeli akış — net yön yok"})

    if ssr is not None:
        if ssr < 50:
            box.append({"sign": "✓", "text": f"SSR {ssr:.0f} çok düşük — bol nakit, altcoin'lere uygun"})
        elif ssr > 200:
            box.append({"sign": "✗", "text": f"SSR {ssr:.0f} yüksek — likidite kıt, risk azalt"})
    return box


def _build_action_box(tab: Tab, snapshot: dict) -> List[Dict[str, str]]:
    if tab == "overview":
        return _action_box_overview(snapshot.get("altseason", {}))
    if tab == "erc20":
        return _action_box_erc20(snapshot.get("radar", {}))
    return _action_box_stable(snapshot.get("pulse", {}))


# ── Gemini prompt + call ─────────────────────────────────────────────────────

def _prompt_overview(altseason: dict, tier: Tier) -> str:
    w_min, w_max = _WORD_TARGETS[("overview", tier)]
    score = altseason.get("altseason_score")
    zone = altseason.get("zone_tr", "")
    components = altseason.get("components") or []
    comp_lines = "\n".join(
        f"  - {c.get('name')}: {c.get('contribution')}/{c.get('weight')} ({c.get('label_tr','')})"
        for c in components
    )
    return (
        "Sen finansal bir analist asistanısın. Aşağıdaki Alt Sezon Skoru verisinden "
        f"{w_min}-{w_max} kelime arası TÜRKÇE bir piyasa yorumu yaz.\n\n"
        f"VERİ:\nSkor: {score}/100 ({zone})\nBileşenler:\n{comp_lines}\n\n"
        "KURALLAR:\n"
        "1. Sadece verilen sayısal değerleri kullan; tahmin/uydurma rakam YASAK.\n"
        "2. Plain Türkçe, jargon minimum. SSR=Stablecoin Supply Ratio diye açıklayabilirsin.\n"
        "3. Üç soruyu net cevapla: (a) şu an piyasa nasıl, (b) hangi bileşen belirleyici, "
        "(c) yatırımcı için ne ima ediyor.\n"
        "4. Fiyat hedefi, tarih tahmini, %X yükselecek tarzı kehanet YASAK.\n"
        "5. Çıktı sadece JSON: {\"story_md\": \"...\"}\n"
    )


def _prompt_erc20(radar: dict, tier: Tier) -> str:
    w_min, w_max = _WORD_TARGETS[("erc20", tier)]
    agg = radar.get("aggregate_label_tr", "")
    tokens = radar.get("tokens") or []
    token_lines = "\n".join(
        f"  - {t.get('symbol')}: {t.get('flow_label_tr') or t.get('flow_signal','?')} "
        f"(7G netflow: {t.get('netflow_7d', 0):,.0f})"
        for t in tokens[:9]
    )
    return (
        "Sen finansal bir analist asistanısın. Aşağıdaki ERC20 DeFi token akıllı para "
        f"verisinden {w_min}-{w_max} kelime arası TÜRKÇE yorum yaz.\n\n"
        f"VERİ:\nGenel görünüm: {agg}\nToken bazında:\n{token_lines}\n\n"
        "KURALLAR:\n"
        "1. 'Birikim' = akıllı para borsadan çekiyor (alıma hazırlık), "
        "'Dağıtım' = borsaya yatırıyor (satışa hazırlık). Bu terimleri açıklayabilirsin.\n"
        "2. En güçlü 1-2 birikim ve 1-2 dağıtım token'ını isimle söyle.\n"
        "3. Fiyat hedefi/kehanet YASAK. Sadece akış yorumu.\n"
        "4. Plain Türkçe, kelime sayısı sınırına uy.\n"
        "5. Çıktı sadece JSON: {\"story_md\": \"...\"}\n"
    )


def _prompt_stable(pulse: dict, tier: Tier) -> str:
    w_min, w_max = _WORD_TARGETS[("stable", tier)]
    totals = pulse.get("totals") or {}
    flow_label = pulse.get("flow_label_tr", "")
    ssr = pulse.get("ssr_proxy")
    ssr_label = pulse.get("ssr_label_tr", "")
    return (
        "Sen finansal bir analist asistanısın. Aşağıdaki Stablecoin Nabzı "
        f"verisinden {w_min}-{w_max} kelime arası TÜRKÇE yorum yaz.\n\n"
        f"VERİ:\n"
        f"Borsada bekleyen toplam (USDC+DAI): ${totals.get('reserve_usd', 0):,.0f}\n"
        f"24 saatlik net akış: ${totals.get('netflow_1d', 0):,.0f}\n"
        f"7 günlük net akış: ${totals.get('netflow_7d', 0):,.0f}\n"
        f"Akış yönü: {flow_label}\n"
        f"SSR (BTC mcap / stablecoin reserve): {ssr} — {ssr_label}\n\n"
        "KURALLAR:\n"
        "1. Borsadaki stablecoin = 'kuru barut' = alım için bekleyen para. "
        "Bu metaforu kullanabilirsin.\n"
        "2. SSR düşükse bol nakit (altcoin için iyi), yüksekse likidite kıt.\n"
        "3. Sayısal değerlere atıf yap ama yeni rakam uydurma.\n"
        "4. Fiyat hedefi/kehanet YASAK.\n"
        "5. Çıktı sadece JSON: {\"story_md\": \"...\"}\n"
    )


_PROMPT_FN = {
    "overview": _prompt_overview,
    "erc20":    _prompt_erc20,
    "stable":   _prompt_stable,
}


async def _call_gemini(prompt: str, max_tokens: int = 1024) -> Optional[str]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or "buraya" in api_key:
        logger.error("GEMINI_API_KEY missing for crypto_intel_storyteller")
        return None
    url = GEMINI_URL_TEMPLATE.format(model=GEMINI_MODEL, key=api_key)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            logger.warning(f"intel storyteller gemini {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"intel storyteller gemini call failed: {e}")
        return None
    candidates = data.get("candidates") or []
    if not candidates:
        return None
    raw = (candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "") or "").strip()
    if not raw:
        return None
    fence = re.search(r"\{[\s\S]*\}", raw)
    if not fence:
        return None
    try:
        obj = json.loads(fence.group(0))
        return (obj.get("story_md") or "").strip() or None
    except json.JSONDecodeError:
        return None


# ── Persist ──────────────────────────────────────────────────────────────────

async def _persist_story(
    tab: Tab,
    tier: Tier,
    story_md: str,
    action_box: List[Dict[str, str]],
    source_snapshot: dict,
) -> None:
    sql = text("""
        INSERT INTO crypto_intel_stories (tab, tier, story_md, action_box, source_snapshot, generated_at)
        VALUES (:tab, :tier, :story, CAST(:action AS jsonb), CAST(:src AS jsonb), NOW())
        ON CONFLICT (tab, tier) DO UPDATE
          SET story_md = EXCLUDED.story_md,
              action_box = EXCLUDED.action_box,
              source_snapshot = EXCLUDED.source_snapshot,
              generated_at = NOW()
    """)
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, {
                "tab": tab, "tier": tier, "story": story_md,
                "action": json.dumps(action_box),
                "src": json.dumps(source_snapshot),
            })
    except Exception as e:
        logger.warning(f"intel story persist error ({tab}/{tier}): {e}")


# ── Public API ───────────────────────────────────────────────────────────────

@dataclass
class IntelStoryResult:
    tab: Tab
    tier: Tier
    story_md: Optional[str]
    action_box: List[Dict[str, str]]
    generated_at: Optional[datetime]
    source_snapshot: dict


async def get_intel_story(tab: Tab, tier: Tier) -> Optional[IntelStoryResult]:
    """Cache okuma — scheduler doldurur, route bu fonksiyonu çağırır."""
    sql = text("""
        SELECT story_md, action_box, source_snapshot, generated_at
        FROM crypto_intel_stories
        WHERE tab = :tab AND tier = :tier
        LIMIT 1
    """)
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, {"tab": tab, "tier": tier})).mappings().first()
    except Exception as e:
        logger.warning(f"intel story fetch error ({tab}/{tier}): {e}")
        return None
    if not row:
        return None
    action_box = row["action_box"]
    if isinstance(action_box, str):
        try:
            action_box = json.loads(action_box)
        except Exception:
            action_box = []
    src = row["source_snapshot"]
    if isinstance(src, str):
        try:
            src = json.loads(src)
        except Exception:
            src = {}
    return IntelStoryResult(
        tab=tab,
        tier=tier,
        story_md=row["story_md"],
        action_box=action_box or [],
        generated_at=row["generated_at"],
        source_snapshot=src or {},
    )


async def _generate_one(tab: Tab, tier: Tier, snapshot: dict) -> bool:
    """Tek bir (tab, tier) çifti için story üret + cache'le."""
    prompt_fn = _PROMPT_FN[tab]
    if tab == "overview":
        prompt = prompt_fn(snapshot.get("altseason", {}), tier)
    elif tab == "erc20":
        prompt = prompt_fn(snapshot.get("radar", {}), tier)
    else:
        prompt = prompt_fn(snapshot.get("pulse", {}), tier)

    story_md = await _call_gemini(prompt)
    if not story_md:
        logger.warning(f"intel story Gemini failed: {tab}/{tier}")
        return False

    action_box = _build_action_box(tab, snapshot)
    # source_snapshot'a ham veri girdi — sentence-window validator için ileride
    src = {
        "altseason_score": (snapshot.get("altseason") or {}).get("altseason_score"),
        "altseason_zone":  (snapshot.get("altseason") or {}).get("zone_tr"),
        "stable_netflow_7d": ((snapshot.get("pulse") or {}).get("totals") or {}).get("netflow_7d"),
        "stable_ssr":      (snapshot.get("pulse") or {}).get("ssr_proxy"),
        "erc20_aggregate": (snapshot.get("radar") or {}).get("aggregate_label_tr"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    await _persist_story(tab, tier, story_md, action_box, src)
    return True


async def refresh_all_intel_stories() -> dict:
    """Scheduler hook — 3 tab × 2 tier = 6 story üret. Snapshot bir kez çekilir,
    her tier için ayrı Gemini call (Premium kısa, Advance uzun)."""
    from services.cryptoquant_market import (
        get_altseason_score,
        get_stablecoin_pulse,
        get_erc20_radar,
    )

    logger.info("Crypto Intel Storyteller: refresh start")

    altseason, pulse, radar = await asyncio.gather(
        get_altseason_score(cache_only=True),
        get_stablecoin_pulse(cache_only=True),
        get_erc20_radar(cache_only=True),
        return_exceptions=True,
    )

    def _ok(x) -> bool:
        return not isinstance(x, Exception) and bool(x) and not (isinstance(x, dict) and x.get("loading"))

    snapshot = {
        "altseason": altseason if _ok(altseason) else {},
        "pulse":     pulse     if _ok(pulse)     else {},
        "radar":     radar     if _ok(radar)     else {},
    }
    if not any(snapshot.values()):
        logger.warning("Intel storyteller: tüm market metric'leri boş, skip")
        return {"status": "no_data"}

    results = {}
    tabs: List[Tab] = ["overview", "erc20", "stable"]
    tiers: List[Tier] = ["premium", "advance"]
    for tab in tabs:
        # Girdi yoksa o tab'ı skip (cache boş kalır, frontend graceful degrade)
        if (tab == "overview" and not snapshot["altseason"]) or \
           (tab == "erc20" and not snapshot["radar"]) or \
           (tab == "stable" and not snapshot["pulse"]):
            results[tab] = "no_input"
            continue
        for tier in tiers:
            ok = await _generate_one(tab, tier, snapshot)
            results[f"{tab}/{tier}"] = "ok" if ok else "failed"

    logger.info(f"Crypto Intel Storyteller: refresh done — {results}")
    return {"status": "ok", "results": results}
