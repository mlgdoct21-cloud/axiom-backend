"""Kurumsal Sentez public endpoint — Commit 4a.

macro_public.py (`_peek_tier` + `/latest`) forku. Optional-auth: anonim/
free → kilitli teaser + upgrade CTA; premium/advance → tam synthesis_md.
Dashboard `/api/corporate/latest` proxy'si (Commit 4b) bunu tüketir.

Salt-okunur (corporate_syntheses); Gemini/yazma YOK. Satır yoksa 200 +
synthesis:null (dashboard "henüz yok" state'i 404'süz render etsin).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, Response
from sqlalchemy import text

from core.database import engine
from core.logger import get_logger
from services.auth import AuthService

logger = get_logger("corporate.public")

router = APIRouter(prefix="/corporate", tags=["corporate"])

_CACHE_HEADER = "public, max-age=60, stale-while-revalidate=120"
_TEASER_CHARS = 280


async def _peek_tier(authorization: Optional[str]) -> str:
    """Bearer token'dan best-effort tier. Hata/eksik → 'free' (401 değil;
    public endpoint, anonim Free preview alır). macro_public forku."""
    if not authorization:
        return "free"
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return "free"
    payload = AuthService.verify_token(parts[1]) or {}
    user_id = payload.get("sub")
    if not user_id:
        return "free"
    try:
        uid_int = int(user_id)
    except (TypeError, ValueError):
        return "free"
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(
                text("SELECT tier FROM users WHERE id = :uid"),
                {"uid": uid_int},
            )).first()
    except Exception as e:  # noqa: BLE001
        logger.info(f"corp _peek_tier skip: {e}")
        return "free"
    if not row or not row[0]:
        return "free"
    tier = str(row[0]).lower().strip()
    return tier if tier in ("free", "premium", "advance") else "free"


async def _load_latest(tier: str) -> Optional[dict]:
    """tier'a uygun en güncel hafta satırı. advance→advance|premium
    fallback; premium→premium; free→premium (teaser için)."""
    want = "advance" if tier == "advance" else "premium"
    sql = text(
        "SELECT event_id, tier, week_start, synthesis_md, source_count, "
        "generated_at FROM corporate_syntheses "
        "WHERE tier = :t ORDER BY week_start DESC, generated_at DESC LIMIT 1"
    )
    try:
        async with engine.begin() as conn:
            row = (await conn.execute(sql, {"t": want})).mappings().first()
            if not row and want == "advance":
                row = (await conn.execute(
                    sql, {"t": "premium"}
                )).mappings().first()
        return dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"corp _load_latest error: {e}")
        return None


@router.get("/latest")
async def latest_synthesis(
    response: Response,
    authorization: Optional[str] = Header(None),
):
    """En güncel haftalık kurumsal sentez. Tier-gated.

    - advance/premium: tam synthesis_md.
    - free/anonim: ilk ~280 char teaser + locked=true + upgrade CTA.
    - satır yok: synthesis=null (200).
    """
    response.headers["Cache-Control"] = _CACHE_HEADER
    tier = await _peek_tier(authorization)
    row = await _load_latest(tier)
    now = datetime.now(timezone.utc).isoformat()

    if not row:
        return {"now": now, "tier": tier, "locked": False,
                "week_start": None, "synthesis": None}

    md = row.get("synthesis_md") or ""
    week_start = row.get("week_start")
    week_s = week_start.isoformat() if hasattr(week_start, "isoformat") else str(week_start)

    if tier in ("premium", "advance"):
        return {
            "now": now, "tier": tier, "locked": False,
            "week_start": week_s,
            "synthesis": {
                "event_id": row.get("event_id"),
                "tier": row.get("tier"),
                "synthesis_md": md,
                "source_count": row.get("source_count"),
                "generated_at": (
                    row["generated_at"].isoformat()
                    if row.get("generated_at") else None
                ),
            },
        }

    # free / anon → kilitli teaser
    teaser = md[:_TEASER_CHARS].rstrip()
    if len(md) > _TEASER_CHARS:
        teaser += "…"
    return {
        "now": now, "tier": "free", "locked": True,
        "week_start": week_s,
        "synthesis": {
            "teaser": teaser,
            "upgrade_cta": "💎 Tam haftalık kurumsal sentez Premium/Advance "
                           "üyelere açıktır.",
        },
    }
