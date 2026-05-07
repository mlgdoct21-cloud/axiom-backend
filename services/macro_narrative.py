"""Macro Narrative Change generator — Gemini one-paragraph + sector chips.

Triggered fire-and-forget from release_detect right after a new
macro_releases row is inserted. Reads:
- the release row itself (actual / prior / type)
- last `macro_market_pricing` snapshot strictly before released_at  → before
- first snapshot strictly after released_at                          → after
- `data/sector_impact_map.yaml` for the category-derived sector lists

Calls Gemini 2.5-flash with a tight JSON contract; post-validates with
number-validator (no fabricated numbers) + sector whitelist (no fabricated
sectors). On any validator failure we discard the LLM output and fall back
to the raw YAML description+mechanism + YAML sector lists, so the row always
ends up with *some* narrative.

UPDATE narrative_md only when NULL — repeat probes never overwrite, and the
admin /narrative endpoint can force re-gen by NULLing the column manually.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.macro_sources.sector_map import SectorImpact, load_sector_map
from services.macro_sources.validators import (
    build_allowed_numbers,
    validate_numbers,
    validate_sectors,
)

logger = get_logger("macro.narrative")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)
_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# Heuristic threshold for HOT/COOL classification on FRED metrics.
# delta% above HOT_THRESHOLD → HOT, below -HOT_THRESHOLD → COOL, else neutral.
_HOT_THRESHOLD_PCT = Decimal("0.20")


@dataclass
class NarrativeResult:
    event_id: str
    written: bool = False
    used_fallback: bool = False
    rejection_reason: Optional[str] = None
    narrative_md: Optional[str] = None
    sentiment_score: Optional[float] = None


# ---------- DB helpers ----------

async def _load_release(event_id: str) -> Optional[dict]:
    sql = text("""
        SELECT event_id, event_type, source, released_at, actual_value,
               prior_value, expected_value, narrative_md, source_url
        FROM macro_releases WHERE event_id = :eid
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
    return dict(row) if row else None


async def _load_market_snapshots(meeting_ticker: Optional[str], around: datetime) -> tuple[Optional[dict], Optional[dict]]:
    """Returns (before, after) Kalshi snapshots straddling `around`."""
    if not meeting_ticker:
        return None, None
    sql_before = text("""
        SELECT modal_rate_pct, modal_prob, snapshot_ts
        FROM macro_market_pricing
        WHERE meeting_ticker = :mt AND snapshot_ts < :ts
        ORDER BY snapshot_ts DESC LIMIT 1
    """)
    sql_after = text("""
        SELECT modal_rate_pct, modal_prob, snapshot_ts
        FROM macro_market_pricing
        WHERE meeting_ticker = :mt AND snapshot_ts > :ts
        ORDER BY snapshot_ts ASC LIMIT 1
    """)
    async with engine.begin() as conn:
        b = (await conn.execute(sql_before, {"mt": meeting_ticker, "ts": around})).mappings().first()
        a = (await conn.execute(sql_after, {"mt": meeting_ticker, "ts": around})).mappings().first()
    return (dict(b) if b else None, dict(a) if a else None)


async def _persist(
    event_id: str,
    narrative_md: str,
    sentiment_score: Optional[float],
    sectors_positive: Optional[list] = None,
    sectors_negative: Optional[list] = None,
) -> bool:
    """Idempotent UPDATE — only fills when narrative_md IS NULL."""
    sql = text("""
        UPDATE macro_releases
        SET narrative_md = :narr,
            sentiment_score = :sent,
            sectors_positive = CAST(:pos AS JSONB),
            sectors_negative = CAST(:neg AS JSONB)
        WHERE event_id = :eid AND narrative_md IS NULL
    """)
    async with engine.begin() as conn:
        result = await conn.execute(
            sql,
            {
                "narr": narrative_md,
                "sent": sentiment_score,
                "pos": json.dumps(list(sectors_positive or [])),
                "neg": json.dumps(list(sectors_negative or [])),
                "eid": event_id,
            },
        )
    return (result.rowcount or 0) > 0


# ---------- Categorisation ----------

def _categorise(release: dict) -> Optional[str]:
    """Pick a sector_impact_map.yaml category from the row's signal."""
    et = (release.get("event_type") or "").upper()
    actual = release.get("actual_value")
    prior = release.get("prior_value")

    if et in ("FOMC_STATEMENT", "FOMC_MINUTES", "FOMC_PROJECTIONS", "RATE_DECISION"):
        # No quantitative direction yet; default to HAWKISH leaning. Day 5+
        # can refine when SEP dot-plot parsing lands. Fallback to HAWKISH so
        # downstream guards always have a category.
        return "FOMC_HAWKISH"

    if actual is None or prior is None:
        return None

    try:
        a = Decimal(str(actual))
        p = Decimal(str(prior))
    except Exception:
        return None
    if p == 0:
        return None
    delta_pct = (a - p) / abs(p) * Decimal("100")

    if et == "CPI":
        if delta_pct > _HOT_THRESHOLD_PCT:
            return "CPI_HOT"
        if delta_pct < -_HOT_THRESHOLD_PCT:
            return "CPI_COOL"
        return "CPI_HOT" if delta_pct >= 0 else "CPI_COOL"
    if et == "CORE_CPI":
        return "CORE_CPI_HOT" if delta_pct >= 0 else "CORE_CPI_COOL"
    if et == "NFP":
        if delta_pct > _HOT_THRESHOLD_PCT:
            return "NFP_HOT"
        if delta_pct < -_HOT_THRESHOLD_PCT:
            return "NFP_COOL"
        return "NFP_HOT" if delta_pct >= 0 else "NFP_COOL"
    if et == "PCE":
        return "PCE_HOT" if delta_pct >= 0 else "PCE_COOL"
    if et == "CORE_PCE":
        return "CORE_PCE_HOT" if delta_pct >= 0 else "CORE_PCE_COOL"
    # Day 28 part 3 — yeni event_type'lar
    if et == "JOBLESS_INITIAL":
        # Düşük claims = sıkı işgücü; "düşük" yön bullish risk varlıklara
        # değil, hawkish Fed'e götürür. delta_pct hafta-üzerinden değişim.
        return "JOBLESS_LOW" if delta_pct < 0 else "JOBLESS_HIGH"
    if et == "JOBLESS_CONTINUING":
        return "JOBLESS_LOW" if delta_pct < 0 else "JOBLESS_HIGH"
    if et == "RETAIL_SALES":
        return "RETAIL_HOT" if delta_pct >= 0 else "RETAIL_COOL"
    if et == "PPI":
        return "PPI_HOT" if delta_pct >= 0 else "PPI_COOL"
    if et == "HOUSING_STARTS":
        return "HOUSING_HOT" if delta_pct >= 0 else "HOUSING_COOL"
    if et == "GDP":
        return "GDP_HOT" if delta_pct >= 0 else "GDP_COOL"
    return None


# ---------- Gemini call ----------

# Event types where Kalshi rate-distribution before/after snapshots make sense
# (markets repricing the meeting outcome). For CPI/NFP/PCE the meeting-rate
# distribution doesn't track the print's expectation, so we suppress those.
_FOMC_EVENT_TYPES = frozenset({
    "FOMC_STATEMENT", "FOMC_MINUTES", "FOMC_PROJECTIONS", "RATE_DECISION",
})


def _format_clause(payload: dict) -> str:
    """Pick the narrative opening that matches the data we actually have.

    - FOMC + Kalshi before/after present → "Piyasa önce X bekliyordu, ŞİMDİ Y"
      (X = before_market.modal_rate_pct, Y = after_market.modal_rate_pct)
    - FRED CPI/NFP with prior + actual → "Önceki ay X iken bu ay Y geldi"
    - FOMC without snapshots → "Fed bugün <event_type> yayınladı. Mekanizma: ..."
    """
    et = (payload.get("event_type") or "").upper()
    has_ba = "before_market" in payload and "after_market" in payload

    if et in _FOMC_EVENT_TYPES and has_ba:
        return (
            "Format: \"Piyasa önce <before_market.modal_rate_pct>% modal oranı "
            "<before_market.modal_prob> olasılıkla bekliyordu; ŞİMDİ "
            "<after_market.modal_rate_pct>% modal oranı "
            "<after_market.modal_prob> olasılıkla bekleniyor.\" Ardından bir "
            "cümlede mekanizmayı özetle (≤30 kelime)."
        )
    if et in ("CPI", "PCE", "CORE_CPI", "CORE_PCE") and payload.get("mom_pct") is not None:
        return (
            "Format: \"Aylık değişim <mom_pct>% (önceki endeks <prior>, mevcut <actual>).\" "
            "Ardından bir cümlede yön + mekanizma (≤30 kelime). "
            "\"Piyasa beklentisi\" ifadesi KULLANMA — INPUT'ta beklenti yok."
        )
    if et == "NFP" and payload.get("prior") is not None and payload.get("actual") is not None:
        return (
            "Format: \"Önceki ay <prior> iken bu ay <actual> kişi geldi.\" "
            "Ardından bir cümlede yön + mekanizma (≤30 kelime). "
            "\"Piyasa beklentisi\" ifadesi KULLANMA — INPUT'ta beklenti yok."
        )
    # Day 28 part 3 — yeni event_type formatları
    if et in ("JOBLESS_INITIAL", "JOBLESS_CONTINUING") and payload.get("actual") is not None and payload.get("prior") is not None:
        label = "İlk işsizlik başvuruları" if et == "JOBLESS_INITIAL" else "Devam eden işsizlik başvuruları"
        return (
            f"Format: \"{label} geçen hafta <prior> iken bu hafta <actual> kişi.\" "
            "Ardından bir cümlede yön + mekanizma (≤30 kelime). "
            "Düşüş = sıkı işgücü, hawkish Fed; artış = soğuyan işgücü, dovish dönüş."
        )
    if et == "RETAIL_SALES" and payload.get("mom_pct") is not None:
        return (
            "Format: \"Perakende satışlar aylık <mom_pct>% (önceki <prior>, mevcut <actual>).\" "
            "Ardından bir cümlede yön + mekanizma (≤30 kelime). Pozitif = sıkı tüketici/sticky CPI; negatif = soğuyan talep."
        )
    if et == "PPI" and payload.get("mom_pct") is not None:
        return (
            "Format: \"Üretici fiyatları aylık <mom_pct>% (önceki <prior>, mevcut <actual>).\" "
            "Ardından bir cümlede yön + mekanizma (≤30 kelime). PPI CPI öncülüdür."
        )
    if et == "HOUSING_STARTS" and payload.get("actual") is not None and payload.get("prior") is not None:
        return (
            "Format: \"Konut başlangıçları önceki <prior> iken bu ay <actual> bin birim.\" "
            "Ardından bir cümlede yön + mekanizma (≤30 kelime). Faiz duyarlı sektör."
        )
    if et == "GDP" and payload.get("actual") is not None and payload.get("prior") is not None:
        return (
            "Format: \"GDP önceki çeyrek <prior>% iken bu çeyrek <actual>%.\" "
            "Ardından bir cümlede yön + mekanizma (≤30 kelime). Büyüme yönü Fed kararını etkiler."
        )
    # FOMC without before/after (or unknown shape): factual + mechanism only.
    return (
        "Format: \"Fed bugün <event_type> yayınladı.\" Ardından bir cümlede "
        "mekanizmayı özetle (≤30 kelime). Sayı yazma — INPUT'ta numeric yok."
    )


def _build_prompt(payload: dict, impact: SectorImpact) -> str:
    format_clause = _format_clause(payload)
    return (
        "Sen makro analist asistanısın. Aşağıdaki JSON release verisini oku ve "
        "**sadece geçerli bir JSON** döndür. Hiçbir açıklama, markdown veya "
        "kod bloğu yazma.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n\n"
        "ÇIKTI ŞEMASI (zorunlu):\n"
        "{\n"
        f'  "narrative_md": "≤80 kelime, Türkçe, tek paragraf. {format_clause} '
        "Sayılar SADECE INPUT'ta geçen değerlerden olmalı; sayı uydurma.\",\n"
        '  "sentiment_score": 0..1 ondalık (1 = çok bullish/dovish risk varlıklarına, 0 = çok bearish/hawkish),\n'
        f'  "sectors_negative": [bu listenin alt kümesi: {list(impact.sectors_negative)}],\n'
        f'  "sectors_positive": [bu listenin alt kümesi: {list(impact.sectors_positive)}]\n'
        "}\n\n"
        "Kurallar:\n"
        "1. narrative_md içindeki HER sayı INPUT JSON'da geçen bir sayı olmalı.\n"
        "2. sectors_* listelerindeki her isim YUKARIDA verilen listeden seçilmiş olmalı.\n"
        "3. Türkçe yaz; emoji veya markdown sembolü kullanma.\n"
        "4. \"Piyasa bekliyordu\" tarzı ifadeyi YALNIZCA INPUT'ta before_market/after_market varsa kullan.\n"
        "5. Çıktı sadece JSON. Hiçbir ek metin ekleme.\n"
    )


async def _call_gemini(prompt: str) -> Optional[dict]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or "buraya" in api_key:
        logger.error("GEMINI_API_KEY missing/invalid for narrative")
        return None
    url = GEMINI_URL_TEMPLATE.format(model=GEMINI_MODEL, key=api_key)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body)
        if resp.status_code != 200:
            logger.warning(f"narrative gemini {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
    except Exception as e:
        logger.warning(f"narrative gemini call failed: {e}")
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
    # Strip ```json ... ``` if model wrapped despite responseMimeType.
    fence = re.search(r"\{[\s\S]*\}", raw)
    if not fence:
        return None
    try:
        return json.loads(fence.group(0))
    except json.JSONDecodeError:
        return None


# ---------- Public entry ----------

async def generate_narrative(event_id: str) -> NarrativeResult:
    """Generate + persist (idempotent) the Narrative Change for one release."""
    result = NarrativeResult(event_id=event_id)
    release = await _load_release(event_id)
    if not release:
        result.rejection_reason = "release row not found"
        return result
    if release.get("narrative_md"):
        result.rejection_reason = "narrative already present"
        return result

    category = _categorise(release)
    if not category:
        result.rejection_reason = "uncategorisable event"
        return result
    impact_map = load_sector_map()
    impact = impact_map.get(category)
    if not impact:
        result.rejection_reason = f"category {category} missing in sector map"
        return result

    # Kalshi rate-distribution snapshots are only semantically valid for FOMC
    # events — for CPI/NFP/PCE, the meeting-rate distribution does NOT track
    # the print's expectation. Suppress before/after for non-FOMC.
    et_upper = (release.get("event_type") or "").upper()
    before, after = (None, None)
    if et_upper in _FOMC_EVENT_TYPES and release.get("released_at"):
        before_after_meeting = None
        try:
            async with engine.begin() as conn:
                row = (await conn.execute(text(
                    "SELECT meeting_ticker FROM macro_market_pricing "
                    "ORDER BY snapshot_ts DESC LIMIT 1"
                ))).mappings().first()
            before_after_meeting = row["meeting_ticker"] if row else None
        except Exception as e:
            logger.debug(f"no kalshi snapshot lookup: {e}")
        before, after = await _load_market_snapshots(
            before_after_meeting, release.get("released_at"),
        )

    actual_f = float(release["actual_value"]) if release.get("actual_value") is not None else None
    prior_f = float(release["prior_value"]) if release.get("prior_value") is not None else None
    mom_pct = None
    if actual_f is not None and prior_f is not None and prior_f != 0:
        mom_pct = round((actual_f - prior_f) / abs(prior_f) * 100, 2)
    payload: dict = {
        "event_id": event_id,
        "event_type": release.get("event_type"),
        "category": category,
        "released_at": release.get("released_at").isoformat() if release.get("released_at") else None,
        "actual": actual_f,
        "prior": prior_f,
    }
    if mom_pct is not None and (release.get("event_type") or "").upper() in (
        "CPI", "PCE", "CORE_CPI", "CORE_PCE",
        # Day 28 part 3 — RETAIL_SALES ve PPI için de MoM% hesaplanır
        "RETAIL_SALES", "PPI",
    ):
        payload["mom_pct"] = mom_pct
    if before:
        payload["before_market"] = {
            "modal_rate_pct": float(before["modal_rate_pct"]) if before.get("modal_rate_pct") is not None else None,
            "modal_prob": float(before["modal_prob"]) if before.get("modal_prob") is not None else None,
        }
    if after:
        payload["after_market"] = {
            "modal_rate_pct": float(after["modal_rate_pct"]) if after.get("modal_rate_pct") is not None else None,
            "modal_prob": float(after["modal_prob"]) if after.get("modal_prob") is not None else None,
        }

    # Build the validator whitelist from every numeric value we fed in.
    allowed_inputs: list = [
        payload.get("actual"), payload.get("prior"), payload.get("mom_pct"),
    ]
    for key in ("before_market", "after_market"):
        if key in payload:
            allowed_inputs += list(payload[key].values())
    allowed_numbers = build_allowed_numbers(allowed_inputs)

    prompt = _build_prompt(payload, impact)
    llm_out = await _call_gemini(prompt)

    if llm_out:
        narrative_md = (llm_out.get("narrative_md") or "").strip()
        sentiment_raw = llm_out.get("sentiment_score")
        sectors_neg = llm_out.get("sectors_negative") or []
        sectors_pos = llm_out.get("sectors_positive") or []

        num_unknown = validate_numbers(narrative_md, allowed_numbers) if narrative_md else ["empty"]
        sec_unknown = validate_sectors(category, sectors_neg, sectors_pos)
        if not num_unknown and not sec_unknown and narrative_md:
            try:
                sentiment = float(sentiment_raw) if sentiment_raw is not None else None
            except (TypeError, ValueError):
                sentiment = None
            written = await _persist(
                event_id, narrative_md, sentiment,
                sectors_positive=sectors_pos,
                sectors_negative=sectors_neg,
            )
            result.written = written
            result.narrative_md = narrative_md
            result.sentiment_score = sentiment
            if written:
                _trigger_broadcast(event_id)
            return result
        logger.warning(
            f"narrative reject for {event_id}: nums={num_unknown} sectors={sec_unknown}"
        )
        result.rejection_reason = f"validator: nums={num_unknown} sectors={sec_unknown}"

    # Fallback: ham YAML servis et — tek satır, deterministic, sayısız.
    fallback_md = (
        f"{impact.description} "
        f"Mekanizma: {impact.mechanism}."
    )
    written = await _persist(
        event_id, fallback_md, None,
        sectors_positive=list(impact.sectors_positive),
        sectors_negative=list(impact.sectors_negative),
    )
    result.written = written
    result.used_fallback = True
    result.narrative_md = fallback_md
    if written:
        _trigger_broadcast(event_id)
    return result


def _trigger_broadcast(event_id: str) -> None:
    """Fire-and-forget Telegram broadcast + market reaction capture. Lazy
    import keeps the macro_narrative ↔ macro_broadcaster cycle safe
    (broadcaster also uses the engine).
    """
    try:
        from services.macro_broadcaster import broadcast_release_safe
        asyncio.create_task(broadcast_release_safe(event_id))
    except Exception as e:
        logger.error(f"broadcast trigger failed for {event_id}: {e}")
    try:
        from services.macro_market_reaction import trigger_reaction
        trigger_reaction(event_id)
    except Exception as e:
        logger.error(f"market reaction trigger failed for {event_id}: {e}")


async def generate_narrative_safe(event_id: str) -> None:
    """Fire-and-forget wrapper used by release_detect — never raises."""
    try:
        await generate_narrative(event_id)
    except Exception as e:
        logger.error(f"generate_narrative crashed for {event_id}: {e}")
