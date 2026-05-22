"""Public Macro router — read-only endpoints consumed by the dashboard chip.

No auth: these power the public Macro Pulse mini-chip and its release-detail
modal. Admin endpoints in `macro_admin.py` keep BOT_INTERNAL_SECRET gating.

Two endpoints:
- GET /macro/latest         single most-recent release w/ narrative
- GET /macro/recent?days=14 list of recent releases (chip drill-down list)

Cache headers are short (60s) — releases drop hourly at most, but the chip
should feel fresh; the dashboard hits this on load and on its own refresh tick.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, Query, Request, Response
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.auth import AuthService

logger = get_logger("macro_public")


async def _peek_tier(authorization: Optional[str]) -> str:
    """Best-effort tier read from Bearer token. Returns 'free' on any failure.

    Public macro/story endpoint is **optional-auth** — anonymous callers get
    the Free preview + upgrade CTA, JWT'li çağrılar Premium/Advance içeriği
    görür. JWT eksik/bozuksa 401 fırlatmak yerine 'free' davranırız.
    """
    if not authorization:
        return "free"
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return "free"
    token = parts[1]
    payload = AuthService.verify_token(token) or {}
    user_id = payload.get("sub")
    if not user_id:
        return "free"
    try:
        uid_int = int(user_id)
    except (TypeError, ValueError):
        return "free"
    sql = text("SELECT tier FROM users WHERE id = :uid")
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"uid": uid_int})).first()
    if not row or not row[0]:
        return "free"
    tier = str(row[0]).lower().strip()
    return tier if tier in ("free", "premium", "advance") else "free"

router = APIRouter(prefix="/macro", tags=["macro"])

_CACHE_HEADER = "public, max-age=60, stale-while-revalidate=120"


# Event types where actual/prior are price indices and MoM/YoY % are the
# relevant readings. Headline ↔ Core pairs share the same observation period
# so we look up the paired Core release by date.
_PCT_DELTA_EVENTS = frozenset({"CPI", "PCE", "CORE_CPI", "CORE_PCE"})

# Headline → paired sibling event_type for the same period:
#  - CPI / PCE pair with their Core stripped-out indices (same shape).
#  - NFP pairs with UNRATE — different shape (jobs added vs unemployment %)
#    but they're released together every Employment Situation report and
#    we want them rendered side-by-side in the modal & Telegram.
_CORE_PAIR = {"CPI": "CORE_CPI", "PCE": "CORE_PCE", "NFP": "UNRATE"}

# NFP convention: report monthly *change* in jobs (in thousands).
#   actual_value = total nonfarm payrolls in thousands (e.g. 158637)
#   prior_value  = previous month's total
#   change_k     = actual - prior  (e.g. +137 means "+137K jobs added")
_NFP_EVENTS = frozenset({"NFP"})


def _coerce_jsonb_list(v) -> list:
    """JSONB columns can come back as list (asyncpg) or str (string driver).
    Normalise to a plain list of strings; empty fallback on anything weird.
    """
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str):
        try:
            import json as _json
            parsed = _json.loads(v)
            return [str(x) for x in parsed if x] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _pct(actual: Optional[float], reference: Optional[float]) -> Optional[float]:
    if actual is None or reference is None or reference == 0:
        return None
    return round((actual - reference) / abs(reference) * 100, 2)


async def _fetch_market_reaction(conn, event_id: str) -> Optional[dict]:
    """Look up T+0 + T+5min snapshots for a release and return deltas:
    DXY/SPY as %, US10Y as bp (basis points). Returns None when neither
    snapshot is available — the broadcaster + modal then skip the line.
    """
    sql = text("""
        SELECT t_offset_seconds, dxy, spy, us10y, taken_at
        FROM macro_release_market_snapshots
        WHERE event_id = :eid
        ORDER BY t_offset_seconds ASC
    """)
    rows = (await conn.execute(sql, {"eid": event_id})).mappings().all()
    if not rows:
        return None
    by_offset = {r["t_offset_seconds"]: r for r in rows}
    t0 = by_offset.get(0)
    t5 = by_offset.get(300)
    if not t0 or not t5:
        # Have one but not both — show absolute T+0 only.
        ref = t0 or t5
        return {
            "t_offset_seconds": int(ref["t_offset_seconds"]),
            "dxy_change_pct": None,
            "spy_change_pct": None,
            "us10y_change_bp": None,
            "snapshot": {
                "dxy": float(ref["dxy"]) if ref["dxy"] is not None else None,
                "spy": float(ref["spy"]) if ref["spy"] is not None else None,
                "us10y": float(ref["us10y"]) if ref["us10y"] is not None else None,
            },
        }

    def _pct_delta(a, b):
        if a is None or b is None or b == 0:
            return None
        return round((float(a) - float(b)) / abs(float(b)) * 100, 3)

    def _bp_delta(a, b):
        if a is None or b is None:
            return None
        # ^TNX value is in % (4.25 = 4.25%); 1 bp = 0.01%.
        return round((float(a) - float(b)) * 100, 1)

    return {
        "t_offset_seconds": 300,
        "dxy_change_pct": _pct_delta(t5["dxy"], t0["dxy"]),
        "spy_change_pct": _pct_delta(t5["spy"], t0["spy"]),
        "us10y_change_bp": _bp_delta(t5["us10y"], t0["us10y"]),
        "snapshot": {
            "dxy": float(t5["dxy"]) if t5["dxy"] is not None else None,
            "spy": float(t5["spy"]) if t5["spy"] is not None else None,
            "us10y": float(t5["us10y"]) if t5["us10y"] is not None else None,
        },
    }


async def _fetch_history_value(
    conn, event_type: str, source: str, target_date: datetime,
) -> Optional[float]:
    """Look up actual_value of a release older than `target_date` for the same
    event_type+source. We sort DESC and pick the first row whose released_at
    is at-or-before target_date — handles both 1-month-prior (for prior_pct)
    and 12-month-prior (for YoY) by passing different target_date values.
    """
    sql = text("""
        SELECT actual_value FROM macro_releases
        WHERE event_type = :et AND source = :src
          AND released_at <= :ts
          AND actual_value IS NOT NULL
        ORDER BY released_at DESC LIMIT 1
    """)
    row = (await conn.execute(sql, {"et": event_type, "src": source, "ts": target_date})).first()
    if row is None:
        return None
    return float(row[0]) if row[0] is not None else None


async def _enrich_release(conn, r) -> dict:
    """Compute all derivative %s and the paired Core release on top of a row."""
    actual = float(r["actual_value"]) if r["actual_value"] is not None else None
    prior = float(r["prior_value"]) if r["prior_value"] is not None else None
    et = (r["event_type"] or "").upper()
    src = r["source"] or ""
    released_at = r["released_at"]

    mom_pct: Optional[float] = None
    yoy_pct: Optional[float] = None
    prior_mom_pct: Optional[float] = None
    prior_yoy_pct: Optional[float] = None
    change_k: Optional[float] = None
    prior_change_k: Optional[float] = None
    if et in _NFP_EVENTS and actual is not None and prior is not None:
        change_k = round(actual - prior, 1)
        # Previous month's NFP change → look up T-1 actual + T-2 actual.
        if released_at is not None:
            from datetime import timedelta
            prev_target = released_at - timedelta(days=20)
            prev_actual = await _fetch_history_value(conn, et, src, prev_target)
            if prev_actual is not None:
                prev_prior_target = released_at - timedelta(days=50)
                prev_prior = await _fetch_history_value(conn, et, src, prev_prior_target)
                if prev_prior is not None:
                    prior_change_k = round(prev_actual - prev_prior, 1)
    if et in _PCT_DELTA_EVENTS:
        mom_pct = _pct(actual, prior)
        # 12-mo-prior: query historical row from ~365 days before released_at.
        if released_at is not None:
            from datetime import timedelta
            yoy_target = released_at - timedelta(days=350)
            yr_ago = await _fetch_history_value(conn, et, src, yoy_target)
            yoy_pct = _pct(actual, yr_ago)
            # Previous month's MoM = look up T-1 row, then compute its mom.
            prev_target = released_at - timedelta(days=20)
            prev_actual = await _fetch_history_value(conn, et, src, prev_target)
            if prev_actual is not None:
                prev_prior_target = released_at - timedelta(days=50)
                prev_prior = await _fetch_history_value(conn, et, src, prev_prior_target)
                prior_mom_pct = _pct(prev_actual, prev_prior)
                if prev_actual is not None:
                    prev_yr_target = released_at - timedelta(days=380)
                    prev_yr_ago = await _fetch_history_value(conn, et, src, prev_yr_target)
                    prior_yoy_pct = _pct(prev_actual, prev_yr_ago)

    expected_mom_pct = (
        float(r["expected_mom_pct"]) if "expected_mom_pct" in r and r["expected_mom_pct"] is not None
        else None
    )
    expected_yoy_pct = (
        float(r["expected_yoy_pct"]) if "expected_yoy_pct" in r and r["expected_yoy_pct"] is not None
        else None
    )

    surprise_mom_pp: Optional[float] = None
    surprise_yoy_pp: Optional[float] = None
    if mom_pct is not None and expected_mom_pct is not None:
        surprise_mom_pp = round(mom_pct - expected_mom_pct, 2)
    if yoy_pct is not None and expected_yoy_pct is not None:
        surprise_yoy_pp = round(yoy_pct - expected_yoy_pct, 2)

    market_reaction = await _fetch_market_reaction(conn, r["event_id"])

    return {
        "event_id": r["event_id"],
        "event_type": r["event_type"],
        "country": r["country"],
        "source": r["source"],
        "released_at": released_at.isoformat() if released_at else None,
        "actual_value": actual,
        "prior_value": prior,
        "mom_pct": mom_pct,
        "yoy_pct": yoy_pct,
        "prior_mom_pct": prior_mom_pct,
        "prior_yoy_pct": prior_yoy_pct,
        "change_k": change_k,
        "prior_change_k": prior_change_k,
        "expected_mom_pct": expected_mom_pct,
        "expected_yoy_pct": expected_yoy_pct,
        "surprise_mom_pp": surprise_mom_pp,
        "surprise_yoy_pp": surprise_yoy_pp,
        "market_reaction": market_reaction,
        "narrative_md": r["narrative_md"],
        "sentiment_score": float(r["sentiment_score"]) if r["sentiment_score"] is not None else None,
        "source_url": r["source_url"],
        "sectors_positive": _coerce_jsonb_list(r["sectors_positive"] if "sectors_positive" in r else None),
        "sectors_negative": _coerce_jsonb_list(r["sectors_negative"] if "sectors_negative" in r else None),
    }


_SELECT_COLS = (
    "event_id, event_type, country, source, released_at, "
    "actual_value, prior_value, narrative_md, sentiment_score, source_url, "
    "sectors_positive, sectors_negative, expected_mom_pct, expected_yoy_pct"
)


async def _fetch_paired_core(conn, headline) -> Optional[dict]:
    """If `headline` is CPI/PCE, look up its Core sibling for the same period.
    Returns enriched dict or None if no paired row exists.
    """
    et = (headline["event_type"] or "").upper()
    pair_et = _CORE_PAIR.get(et)
    if not pair_et or not headline["released_at"]:
        return None
    sql = text(f"""
        SELECT {_SELECT_COLS}
        FROM macro_releases
        WHERE event_type = :et AND source = :src AND released_at = :ts
        LIMIT 1
    """)
    row = (await conn.execute(sql, {
        "et": pair_et, "src": headline["source"], "ts": headline["released_at"],
    })).mappings().first()
    if not row:
        return None
    return await _enrich_release(conn, row)


@router.get("/latest")
async def latest_release(response: Response):
    """Single most-recent release that has a narrative_md filled in.

    Sort intent: "most recently broadcast" wins. Multiple sibling releases
    share the same released_at (FRED stores observation-period start, e.g.
    NFP, CPI, PCE for March all sit on 2026-03-01); without a broadcast
    timestamp, /macro/latest was tied on released_at and fell back to
    created_at, so a PCE row whose narrative was regenerated on probe could
    outrank a freshly-broadcast NFP. `last_broadcast_at` is bumped at the
    top of macro_broadcaster.broadcast_release so any re-send promotes the
    row to the top of this list. Coalesce to created_at for legacy rows
    that were ingested before the column existed.

    180-day window prevents an ancient rich row from sticking forever.
    Returns 200 with `release: null` rather than 404 when there's nothing
    yet, so the dashboard chip can render an "Henüz yeni release yok" state
    without triggering an error path.
    """
    response.headers["Cache-Control"] = _CACHE_HEADER
    sql = text(f"""
        SELECT {_SELECT_COLS}
        FROM macro_releases
        WHERE narrative_md IS NOT NULL
          AND released_at >= NOW() - INTERVAL '180 days'
          AND event_type NOT LIKE 'CORE_%'
        ORDER BY (actual_value IS NOT NULL) DESC,
                 COALESCE(last_broadcast_at, created_at) DESC,
                 released_at DESC NULLS LAST
        LIMIT 1
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql)).mappings().first()
        if not row:
            return {
                "now": datetime.now(timezone.utc).isoformat(),
                "release": None,
                "core_release": None,
            }
        enriched = await _enrich_release(conn, row)
        core = await _fetch_paired_core(conn, row)
    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "release": enriched,
        "core_release": core,
    }


@router.get("/recent")
async def recent_releases(
    response: Response,
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(20, ge=1, le=100),
):
    """Recent releases (with or without narrative). Powers the modal drill-down."""
    response.headers["Cache-Control"] = _CACHE_HEADER
    sql = text(f"""
        SELECT {_SELECT_COLS}
        FROM macro_releases
        WHERE released_at >= NOW() - make_interval(days => :days)
        ORDER BY released_at DESC NULLS LAST, created_at DESC
        LIMIT :limit
    """)
    async with engine.begin() as conn:
        rows = (await conn.execute(sql, {"days": days, "limit": limit})).mappings().all()
        out = [await _enrich_release(conn, r) for r in rows]
    return {
        "days": days,
        "count": len(out),
        "releases": out,
    }


@router.get("/upcoming")
async def upcoming(
    response: Response,
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(6, ge=1, le=50),
):
    """Next N events from the merged calendar (FRED release/dates + manual
    YAML for FOMC). Drives the dashboard's 'Sırada' chip — no auth.

    Cache-Control is short (5 min) — calendar is stable, but we want a
    fresh read after admin /calendar/refresh.
    """
    response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    from services.macro_calendar import upcoming_events
    now = datetime.now(timezone.utc)
    events = await upcoming_events(now, days=days)
    # Filter to strictly-future (drop the post-release tail that the
    # admin endpoint includes). Cap to `limit`.
    future = [e for e in events if e.scheduled_at >= now][:limit]
    return {
        "now": now.isoformat(),
        "count": len(future),
        "events": [
            {
                "event_type": e.event_type,
                "label": e.label,
                "scheduled_at": e.scheduled_at.isoformat(),
                "sources_to_accelerate": list(e.sources_to_accelerate),
            }
            for e in future
        ],
    }


@router.get("/history/{event_type}")
async def history(
    event_type: str,
    response: Response,
    months: int = Query(14, ge=1, le=60),
    source: str = Query("fred"),
):
    """Time series of MoM% / YoY% for a single event_type, oldest-first.
    Powers the dashboard's modal-embedded line chart.

    `event_type` is the canonical label (CPI, CORE_CPI, PCE, CORE_PCE).
    """
    response.headers["Cache-Control"] = _CACHE_HEADER
    et = (event_type or "").upper()
    sql = text("""
        SELECT event_id, released_at, actual_value, prior_value
        FROM macro_releases
        WHERE event_type = :et AND source = :src AND actual_value IS NOT NULL
        ORDER BY released_at DESC
        LIMIT :months
    """)
    async with engine.begin() as conn:
        rows = (await conn.execute(sql, {
            "et": et, "src": source, "months": months,
        })).mappings().all()
        if not rows:
            return {"event_type": et, "source": source, "points": []}
        # Reverse to ascending for chart consumption.
        rows = list(reversed(rows))
        out = []
        for r in rows:
            actual = float(r["actual_value"]) if r["actual_value"] is not None else None
            prior = float(r["prior_value"]) if r["prior_value"] is not None else None
            mom = _pct(actual, prior) if et in _PCT_DELTA_EVENTS else None
            yoy_target = r["released_at"] - timedelta_days(350)
            yr_ago = await _fetch_history_value(conn, et, source, yoy_target)
            yoy = _pct(actual, yr_ago) if et in _PCT_DELTA_EVENTS else None
            change_k = (
                round(actual - prior, 1)
                if (et in _NFP_EVENTS and actual is not None and prior is not None)
                else None
            )
            out.append({
                "event_id": r["event_id"],
                "released_at": r["released_at"].isoformat(),
                "actual_value": actual,
                "prior_value": prior,
                "mom_pct": mom,
                "yoy_pct": yoy,
                "change_k": change_k,
            })
    return {"event_type": et, "source": source, "points": out}


def timedelta_days(days: int):
    """Local alias to keep the import section tidy."""
    from datetime import timedelta
    return timedelta(days=days)


@router.get("/release/{event_id:path}")
async def release_detail(event_id: str, response: Response):
    """Single release lookup by event_id. `:path` lets the colons in
    `fred:CPI:2026-03-01` style IDs pass through unencoded.
    """
    response.headers["Cache-Control"] = _CACHE_HEADER
    sql = text(f"""
        SELECT {_SELECT_COLS}
        FROM macro_releases
        WHERE event_id = :eid
    """)
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"eid": event_id})).mappings().first()
        if not row:
            return {"release": None, "core_release": None}
        enriched = await _enrich_release(conn, row)
        core = await _fetch_paired_core(conn, row)
    return {"release": enriched, "core_release": core}


# ---------- Storyteller (tiered) ----------
#
# Free  → narrative_md (tek paragraf, var olan macro_narrative çıktısı) +
#         upgrade_cta yapısı
# Premium → macro_stories.story_md (tier='premium'), 4-5 paragraf hikaye
# Advance → macro_stories.story_md (tier='advance'), 6-7 paragraf + senaryo
#
# Optional auth: JWT'siz çağrı → tier='free'.

_STORY_CACHE_HEADER = "public, max-age=120, stale-while-revalidate=300"


@router.get("/story/{event_id:path}")
async def get_story(
    event_id: str,
    response: Response,
    authorization: Optional[str] = Header(None),
):
    """Tiered story view. JWT optional — anonymous → free preview.

    Response shape (consistent across tiers):
    {
      "event_id": "...",
      "tier_active": "free|premium|advance",
      "narrative_md": "..." (free hap, her tier'da dolu),
      "story": {                       # null for free
        "tier": "premium|advance",
        "story_md": "...",
        "generated_at": "ISO",
        "sources_cited": ["FRED:CPIAUCSL", "BLS"],
        "sources_registry": {code: {name, url}}
      },
      "upgrade_cta": null | {           # null for advance
        "target_tier": "premium|advance",
        "reason": "..."
      }
    }
    """
    response.headers["Cache-Control"] = _STORY_CACHE_HEADER

    tier = await _peek_tier(authorization)

    sql_release = text("""
        SELECT event_id, event_type, country, released_at,
               narrative_md, source_url
        FROM macro_releases WHERE event_id = :eid
    """)
    async with engine.begin() as conn:
        rel = (await conn.execute(sql_release, {"eid": event_id})).mappings().first()
        if not rel:
            return {"event_id": event_id, "release": None}

        # Premium veya Advance ise macro_stories'i çek
        story_payload = None
        if tier in ("premium", "advance"):
            # Advance kullanıcı önce 'advance' arasın, yoksa 'premium'a fallback
            tier_order = ["advance", "premium"] if tier == "advance" else ["premium"]
            sql_story = text("""
                SELECT tier, story_md, meta, generated_at
                FROM macro_stories
                WHERE event_id = :eid AND tier = ANY(:tiers)
                ORDER BY array_position(:tiers, tier) ASC
                LIMIT 1
            """)
            srow = (await conn.execute(sql_story, {
                "eid": event_id, "tiers": tier_order,
            })).mappings().first()
            if srow:
                meta = srow["meta"] or {}
                if isinstance(meta, str):
                    import json as _json
                    try:
                        meta = _json.loads(meta)
                    except Exception:
                        meta = {}
                story_payload = {
                    "tier": srow["tier"],
                    "story_md": srow["story_md"],
                    "generated_at": srow["generated_at"].isoformat() if srow["generated_at"] else None,
                    "sources_cited": (meta.get("validator", {}) or {}).get("sources_cited", []),
                    "sources_registry": meta.get("sources_registry", {}),
                }

    upgrade_cta = None
    if tier == "free":
        upgrade_cta = {
            "target_tier": "premium",
            "reason": "Tam yorum, ağırlık matematiği, Fed reaksiyonu ve "
                      "portföy etkisi Premium aboneliği ile erişilir.",
        }
    elif tier == "premium" and story_payload and story_payload["tier"] == "premium":
        upgrade_cta = {
            "target_tier": "advance",
            "reason": "Trend matematiği, senaryo hesabı ve revizyon push "
                      "alert'leri Advance aboneliği ile erişilir.",
        }

    return {
        "event_id": event_id,
        "tier_active": tier,
        "event_type": rel["event_type"],
        "country": rel["country"],
        "released_at": rel["released_at"].isoformat() if rel["released_at"] else None,
        "narrative_md": rel["narrative_md"],  # Free hap, her tier'da görünür
        "source_url": rel["source_url"],
        "story": story_payload,
        "upgrade_cta": upgrade_cta,
    }


@router.get("/track-record")
async def macro_track_record(
    response: Response,
    event_type: Optional[str] = Query(None),
    tier: Optional[str] = Query(None, pattern="^(premium|advance)$"),
):
    """Public İsabet Skorboard — manuel olarak validate edilmiş story
    outcomes'tan aggregate hit rate.

    `min_validated=3` altında {"status": "insufficient_data"} döner — yani
    en az 3 hikaye review edilene kadar skorboard kullanılmaz.
    """
    response.headers["Cache-Control"] = "public, max-age=300"  # 5 min
    from services.macro_track_record import get_hit_rate, list_outcomes
    rate = await get_hit_rate(event_type=event_type, tier=tier, min_validated=3)
    recent = await list_outcomes(event_type=event_type, tier=tier, limit=10)
    # Public response — yalnızca validated outcome'ları göster, notes/excerpt'i kırp
    public_recent = []
    for r in recent:
        if r.get("validated_at"):
            public_recent.append({
                "story_event_id": r["story_event_id"],
                "tier": r["tier"],
                "event_type": r["event_type"],
                "predicted_verdict": r["predicted_verdict"],
                "predicted_at": r["predicted_at"],
                "hit_score": r["hit_score"],
            })
    return {
        "aggregate": rate,
        "recent_outcomes": public_recent,
    }


@router.get("/liquidity")
async def macro_global_liquidity(response: Response):
    """
    Global Likidite Endeksi (Fed M2 + ECB Total Assets, USD-converted).
    Makro Pulse chip'inin tertiary satırında özet olarak gösterilir.
    """
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300, stale-while-revalidate=600"
    try:
        from services.macro_liquidity import compute_global_liquidity
        data = await compute_global_liquidity()
        if data is None:
            return {"error": "data_unavailable"}
        return data
    except Exception as e:
        logger.error(f"macro/liquidity error: {e}", exc_info=True)
        return {"error": str(type(e).__name__)}
