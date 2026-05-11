"""Macro Storyteller Track Record / İsabet Skorboard — FAZ D.

Storyteller her hikayede yön tahmini verir ("BTC pozitif olabilir", "hawkish
duruş güçlenebilir", vb). Bu tahminleri sonraki release ile karşılaştırarak
isabet oranı hesaplıyoruz.

İki aşamalı sistem:

1. **Manuel validasyon başlangıcı (FEATURE FLAG OFF)** — kullanıcı 3-5 Mart
   2026 hikayesini elle review eder, /admin/macro/track-record/validate
   endpoint'i üzerinden hit/miss not düşer. Hit rate >%50 görüldüğünde
   `MACRO_TRACK_RECORD_ENABLED=true` ile auto-extract devreye girer.

2. **Auto-extract (FEATURE FLAG ON)** — `extract_verdict_from_story()` her
   yeni hikayede yön tahminini keyword scan ile çıkarır; sonraki release
   geldiğinde `compute_outcome()` hit_score atar.

Verdict labels (controlled vocab):
  - 'bullish_btc'   / 'bearish_btc'    (BTC yönü)
  - 'bullish_usd'   / 'bearish_usd'    (DXY yönü)
  - 'hawkish_fed'   / 'dovish_fed'     (Fed faiz patika)
  - 'risk_on'       / 'risk_off'       (genel risk iştahı)
  - 'inflation_up'  / 'inflation_down' (enflasyon yönü tahmin)
  - 'neutral'                          (yön belirsiz)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text

from core.database import engine
from core.logger import get_logger

logger = get_logger("macro.track_record")


def is_enabled() -> bool:
    """Feature flag — default OFF until manual validation proves >%50 hit rate."""
    return os.getenv("MACRO_TRACK_RECORD_ENABLED", "").lower() in ("1", "true", "yes")


# ---------- Verdict extraction (keyword scan) ----------

# Kalıp ifadeler; her biri (regex, verdict_label). Sıra önemli — özelden
# genele. "Sen-için" bölümü genelde son cümlededir.
_VERDICT_PATTERNS = [
    # BTC yönü
    (r"\bBTC[^.]{0,80}(?:pozitif|yukar[ıi]|destek|risk-on|alım)", "bullish_btc"),
    (r"\bBTC[^.]{0,80}(?:bask[ıi]|aşağ[ıi]|sat[ıi][şs]|risk-off|düş)", "bearish_btc"),
    # USD/DXY
    (r"\b(?:USD|DXY|dolar)[^.]{0,60}(?:güçlü|destek|yukar[ıi])", "bullish_usd"),
    (r"\b(?:USD|DXY|dolar)[^.]{0,60}(?:zay[ıi]f|aşağ[ıi]|bask[ıi])", "bearish_usd"),
    # Fed patika
    (r"\b(?:hawkish|şahin|sıkı(?:laştırma)?)\b", "hawkish_fed"),
    (r"\b(?:dovish|güvercin|gevşeme)\b", "dovish_fed"),
    # Genel risk
    (r"\brisk[\s-]?on\b|\briski[\s-]?al[ıi][şs]\b", "risk_on"),
    (r"\brisk[\s-]?off\b|\bsavunmac[ıi]\b", "risk_off"),
    # Enflasyon
    (r"\benflasyon[^.]{0,40}(?:yukar[ıi]|art[ıi][şs]|hızlan)", "inflation_up"),
    (r"\benflasyon[^.]{0,40}(?:aşağ[ıi]|gerile|yavaş|düş)", "inflation_down"),
]


@dataclass
class VerdictExtraction:
    primary_verdict: Optional[str] = None
    all_matches: list[str] = None
    excerpt: Optional[str] = None  # 'Senin için' bölümünden cümle


def extract_verdict_from_story(story_md: str) -> VerdictExtraction:
    """Story metninden yön tahminini regex ile çıkar.

    'Senin için 1-cümle' bölümünü öncelikle hedefler (yön burada nettir).
    Hiçbir pattern eşleşmezse `primary_verdict=None`.
    """
    if not story_md:
        return VerdictExtraction(all_matches=[])

    # 'Senin için' / 'sen icin' / 'senin icin' başlığını bul
    excerpt = story_md
    m = re.search(r"(?:Senin için|Sen[\s-]?icin|Aklında tut)[^.]*\.?[^.]{0,300}",
                  story_md, re.IGNORECASE)
    if m:
        excerpt = m.group(0)

    matches: list[str] = []
    for pat, label in _VERDICT_PATTERNS:
        if re.search(pat, excerpt, re.IGNORECASE):
            if label not in matches:
                matches.append(label)

    # Tüm story üzerinde de tara (fallback)
    if not matches:
        for pat, label in _VERDICT_PATTERNS:
            if re.search(pat, story_md, re.IGNORECASE):
                if label not in matches:
                    matches.append(label)

    return VerdictExtraction(
        primary_verdict=matches[0] if matches else None,
        all_matches=matches,
        excerpt=excerpt[:200] if excerpt else None,
    )


# ---------- Outcome computation ----------

# Bir verdict'in "ne ile karşılaştırıldığı" — verdict çiftine bir scalar metrik
# atayıp gerçek hareket o çiftin ima ettiği yönde mi diye bakacağız.
# Şimdilik basit: sonraki aynı event_type release'inde actual_value değişim
# yönü +verdict ile uyumluysa hit_score=+1.
_VERDICT_FAMILY = {
    "bullish_btc":     ("price_btc", +1),
    "bearish_btc":     ("price_btc", -1),
    "bullish_usd":     ("price_dxy", +1),
    "bearish_usd":     ("price_dxy", -1),
    "hawkish_fed":     ("fed_funds_change", +1),
    "dovish_fed":      ("fed_funds_change", -1),
    "risk_on":         ("price_btc",  +1),  # proxy
    "risk_off":        ("price_btc",  -1),
    "inflation_up":    ("next_cpi_mom", +1),
    "inflation_down":  ("next_cpi_mom", -1),
}


async def record_predicted_verdict(
    story_event_id: str,
    tier: str,
    event_type: str,
    story_md: str,
    *,
    predicted_at: Optional[datetime] = None,
    horizon_days: int = 30,
    auto_inferred: bool = True,
) -> Optional[str]:
    """Hikaye yazıldığında tahmini çıkar + macro_story_outcomes'a yaz.

    Returns: kaydedilen verdict label (veya None — pattern eşleşmedi).
    """
    if not is_enabled():
        return None
    ex = extract_verdict_from_story(story_md)
    if not ex.primary_verdict:
        logger.info(f"track_record: no verdict extracted from {story_event_id}/{tier}")
        return None
    predicted_at = predicted_at or datetime.now(timezone.utc)
    sql = text("""
        INSERT INTO macro_story_outcomes
        (story_event_id, tier, event_type, predicted_verdict, predicted_at,
         horizon_days, auto_inferred, notes)
        VALUES (:eid, :tier, :et, :verdict, :pat, :horizon, :auto, :notes)
        ON CONFLICT ON CONSTRAINT uq_story_outcomes_story_tier DO NOTHING
    """)
    try:
        async with engine.begin() as conn:
            await conn.execute(sql, {
                "eid": story_event_id, "tier": tier, "et": event_type,
                "verdict": ex.primary_verdict, "pat": predicted_at,
                "horizon": horizon_days, "auto": auto_inferred,
                "notes": ex.excerpt,
            })
        return ex.primary_verdict
    except Exception as e:
        logger.warning(f"track_record record failed for {story_event_id}/{tier}: {e}")
        return None


async def validate_outcome_manually(
    story_event_id: str,
    tier: str,
    hit_score: float,
    *,
    validated_by: str = "admin",
    notes: Optional[str] = None,
    compared_event_id: Optional[str] = None,
    actual_outcome: Optional[dict] = None,
) -> bool:
    """Admin endpoint için manuel hit/miss validation. hit_score ∈ [-1, +1]."""
    sql = text("""
        UPDATE macro_story_outcomes SET
            hit_score = :hs,
            validated_at = NOW(),
            validated_by = :vb,
            notes = COALESCE(:n, notes),
            compared_event_id = COALESCE(:ceid, compared_event_id),
            actual_outcome = COALESCE(CAST(:ao AS JSONB), actual_outcome)
        WHERE story_event_id = :eid AND tier = :tier
    """)
    import json as _json
    async with engine.begin() as conn:
        result = await conn.execute(sql, {
            "hs": hit_score, "vb": validated_by, "n": notes,
            "ceid": compared_event_id,
            "ao": _json.dumps(actual_outcome) if actual_outcome else None,
            "eid": story_event_id, "tier": tier,
        })
    return result.rowcount > 0


async def insert_manual_verdict(
    story_event_id: str,
    tier: str,
    event_type: str,
    predicted_verdict: str,
    *,
    horizon_days: int = 30,
    notes: Optional[str] = None,
) -> bool:
    """Admin endpoint için: feature-flag OFF iken bile manuel verdict ekle.
    Validation aşaması için pre-populated row yaratıyor."""
    sql = text("""
        INSERT INTO macro_story_outcomes
        (story_event_id, tier, event_type, predicted_verdict, predicted_at,
         horizon_days, auto_inferred, notes)
        VALUES (:eid, :tier, :et, :verdict, NOW(), :horizon, FALSE, :notes)
        ON CONFLICT ON CONSTRAINT uq_story_outcomes_story_tier DO NOTHING
    """)
    async with engine.begin() as conn:
        result = await conn.execute(sql, {
            "eid": story_event_id, "tier": tier, "et": event_type,
            "verdict": predicted_verdict, "horizon": horizon_days, "notes": notes,
        })
    return result.rowcount > 0


# ---------- Public read ----------

async def get_hit_rate(
    event_type: Optional[str] = None,
    tier: Optional[str] = None,
    *,
    min_validated: int = 3,
) -> dict:
    """Aggregate isabet oranı. min_validated altında 'insufficient data' döner.

    Returns: {event_type, tier, total_validated, hits, misses, hit_rate_pct}
    """
    where_clauses = ["validated_at IS NOT NULL", "hit_score IS NOT NULL"]
    params: dict = {}
    if event_type:
        where_clauses.append("event_type = :et")
        params["et"] = event_type
    if tier:
        where_clauses.append("tier = :tier")
        params["tier"] = tier
    where_sql = " AND ".join(where_clauses)

    sql = text(f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN hit_score > 0 THEN 1 ELSE 0 END) AS hits,
            SUM(CASE WHEN hit_score < 0 THEN 1 ELSE 0 END) AS misses,
            SUM(CASE WHEN hit_score = 0 THEN 1 ELSE 0 END) AS neutral,
            COALESCE(AVG(hit_score), 0) AS avg_score
        FROM macro_story_outcomes
        WHERE {where_sql}
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, params)).first()
    total = int(row[0] or 0)
    hits = int(row[1] or 0)
    misses = int(row[2] or 0)
    neutral = int(row[3] or 0)
    avg_score = float(row[4] or 0.0)
    if total < min_validated:
        return {
            "event_type": event_type, "tier": tier,
            "total_validated": total, "hits": hits, "misses": misses,
            "neutral": neutral,
            "hit_rate_pct": None,
            "avg_score": None,
            "status": "insufficient_data",
            "min_required": min_validated,
        }
    hit_rate_pct = round(hits / total * 100, 1) if total else 0.0
    return {
        "event_type": event_type, "tier": tier,
        "total_validated": total, "hits": hits, "misses": misses,
        "neutral": neutral,
        "hit_rate_pct": hit_rate_pct,
        "avg_score": round(avg_score, 3),
        "status": "ok",
    }


async def list_outcomes(
    event_type: Optional[str] = None,
    tier: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """List recent outcomes (for admin review UI / public scoreboard)."""
    where = []
    params: dict = {"limit": limit}
    if event_type:
        where.append("event_type = :et"); params["et"] = event_type
    if tier:
        where.append("tier = :tier"); params["tier"] = tier
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = text(f"""
        SELECT story_event_id, tier, event_type, predicted_verdict,
               predicted_at, horizon_days, compared_event_id,
               actual_outcome, hit_score, auto_inferred,
               validated_at, validated_by, notes
        FROM macro_story_outcomes
        {where_sql}
        ORDER BY predicted_at DESC
        LIMIT :limit
    """)
    async with engine.begin() as conn:
        rows = (await conn.execute(sql, params)).mappings().all()
    out = []
    for r in rows:
        out.append({
            "story_event_id": r["story_event_id"],
            "tier": r["tier"],
            "event_type": r["event_type"],
            "predicted_verdict": r["predicted_verdict"],
            "predicted_at": r["predicted_at"].isoformat() if r["predicted_at"] else None,
            "horizon_days": r["horizon_days"],
            "compared_event_id": r["compared_event_id"],
            "actual_outcome": r["actual_outcome"],
            "hit_score": float(r["hit_score"]) if r["hit_score"] is not None else None,
            "auto_inferred": r["auto_inferred"],
            "validated_at": r["validated_at"].isoformat() if r["validated_at"] else None,
            "validated_by": r["validated_by"],
            "notes": r["notes"],
        })
    return out
