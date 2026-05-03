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


async def _persist(event_id: str, narrative_md: str, sentiment_score: Optional[float]) -> bool:
    """Idempotent UPDATE — only fills when narrative_md IS NULL."""
    sql = text("""
        UPDATE macro_releases
        SET narrative_md = :narr, sentiment_score = :sent
        WHERE event_id = :eid AND narrative_md IS NULL
    """)
    async with engine.begin() as conn:
        result = await conn.execute(
            sql,
            {"narr": narrative_md, "sent": sentiment_score, "eid": event_id},
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
    if et == "NFP":
        if delta_pct > _HOT_THRESHOLD_PCT:
            return "NFP_HOT"
        if delta_pct < -_HOT_THRESHOLD_PCT:
            return "NFP_COOL"
        return "NFP_HOT" if delta_pct >= 0 else "NFP_COOL"
    if et == "PCE":
        return "PCE_HOT" if delta_pct >= 0 else "PCE_COOL"
    return None


# ---------- Gemini call ----------

def _build_prompt(payload: dict, impact: SectorImpact) -> str:
    return (
        "Sen makro analist asistanısın. Aşağıdaki JSON release verisini oku ve "
        "**sadece geçerli bir JSON** döndür. Hiçbir açıklama, markdown veya "
        "kod bloğu yazma.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}\n\n"
        "ÇIKTI ŞEMASI (zorunlu):\n"
        "{\n"
        '  "narrative_md": "≤80 kelime, Türkçe, format: \\"Piyasa önce X bekliyordu, ŞİMDİ Y bekliyor\\". '
        "Sayılar SADECE INPUT'ta geçen değerlerden olmalı; sayı uydurma. Tek paragraf.\",\n"
        '  "sentiment_score": 0..1 ondalık (1 = çok bullish/dovish risk varlıklarına, 0 = çok bearish/hawkish),\n'
        f'  "sectors_negative": [bu listenin alt kümesi: {list(impact.sectors_negative)}],\n'
        f'  "sectors_positive": [bu listenin alt kümesi: {list(impact.sectors_positive)}]\n'
        "}\n\n"
        "Kurallar:\n"
        "1. narrative_md içindeki HER sayı INPUT JSON'da geçen bir sayı olmalı.\n"
        "2. sectors_* listelerindeki her isim YUKARIDA verilen listeden seçilmiş olmalı.\n"
        "3. Türkçe yaz; emoji veya markdown sembolü kullanma.\n"
        "4. Çıktı sadece JSON. Hiçbir ek metin ekleme.\n"
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

    # Best-effort market snapshots: only meaningful when an open KXFED meeting
    # is "linked" to the release. v0: try the next-meeting ticker globally, and
    # let the LLM omit before/after if we couldn't supply both.
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
    ) if release.get("released_at") else (None, None)

    payload: dict = {
        "event_id": event_id,
        "event_type": release.get("event_type"),
        "category": category,
        "released_at": release.get("released_at").isoformat() if release.get("released_at") else None,
        "actual": float(release["actual_value"]) if release.get("actual_value") is not None else None,
        "prior": float(release["prior_value"]) if release.get("prior_value") is not None else None,
    }
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
        payload.get("actual"), payload.get("prior"),
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
            written = await _persist(event_id, narrative_md, sentiment)
            result.written = written
            result.narrative_md = narrative_md
            result.sentiment_score = sentiment
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
    written = await _persist(event_id, fallback_md, None)
    result.written = written
    result.used_fallback = True
    result.narrative_md = fallback_md
    return result


async def generate_narrative_safe(event_id: str) -> None:
    """Fire-and-forget wrapper used by release_detect — never raises."""
    try:
        await generate_narrative(event_id)
    except Exception as e:
        logger.error(f"generate_narrative crashed for {event_id}: {e}")
