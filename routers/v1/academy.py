"""Opsiyon Akademisi public router — Faz 1.

Müfredat / sözlük / senaryo kartı / theta-wheel okumaları **optional-auth**:
JWT yoksa free olarak servis edilir, JWT varsa user.tier'a göre kilit açılır.
Progress endpoint'leri zorunlu auth.

Read endpoint cache header'ları kısa (5dk) — içerik statik ama tier filtresi
header-bağımlı, dolayısıyla `private` cache. Glossary statik olduğu için
daha uzun cache verilebilir.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import engine, get_db
from core.logger import get_logger
from core.security import get_current_user
from services.auth import AuthService
from services import academy_service

logger = get_logger("academy_router")

router = APIRouter(prefix="/academy", tags=["academy"])

# Lokal geliştirme kolaylığı: AXIOM_LOCAL_DEV=1 SADECE lokal .env'de set'lidir
# (Railway prod'da YOKTUR). Set'liyken akademiyi giriş yapmadan, tüm premium/advance
# içerik açık şekilde önizleyebilmek için tier'ı 'advance' kabul ederiz. Prod'u
# ETKİLEMEZ — bayrak orada tanımlı olmadığı için normal JWT akışı çalışır.
_LOCAL_DEV_TIER: Optional[str] = (
    "advance" if os.getenv("AXIOM_LOCAL_DEV", "").strip().lower() in ("1", "true", "yes") else None
)


async def _peek_tier(authorization: Optional[str]) -> str:
    """Best-effort tier read (macro_public.py ile aynı kalıp).
    JWT yoksa/bozuksa 'free' döner — 401 fırlatmaz."""
    if _LOCAL_DEV_TIER:
        return _LOCAL_DEV_TIER
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
    sql = text("SELECT tier FROM users WHERE id = :uid")
    async with engine.begin() as conn:
        row = (await conn.execute(sql, {"uid": uid_int})).first()
    if not row or not row[0]:
        return "free"
    tier = str(row[0]).lower().strip()
    return tier if tier in ("free", "premium", "advance") else "free"


_PRIVATE_CACHE = "private, max-age=300"
_PUBLIC_CACHE = "public, max-age=900, stale-while-revalidate=1800"


# ---------------------------------------------------------------------------
# Content endpoints (optional auth)
# ---------------------------------------------------------------------------


@router.get("/curriculum")
async def get_curriculum(
    response: Response,
    authorization: Optional[str] = Header(default=None),
):
    """Tier-aware modül listesi. Free kullanıcı M2-M4 için kilitli teaser görür."""
    tier = await _peek_tier(authorization)
    response.headers["Cache-Control"] = _PRIVATE_CACHE
    return academy_service.get_curriculum_summary(tier)


@router.get("/modules/{module_id}")
async def get_module(
    module_id: str,
    response: Response,
    authorization: Optional[str] = Header(default=None),
):
    tier = await _peek_tier(authorization)
    data = academy_service.get_module(module_id, tier)
    if not data:
        raise HTTPException(status_code=404, detail="module not found")
    response.headers["Cache-Control"] = _PRIVATE_CACHE
    return data


@router.get("/lessons/{lesson_id}")
async def get_lesson(
    lesson_id: str,
    response: Response,
    authorization: Optional[str] = Header(default=None),
):
    tier = await _peek_tier(authorization)
    data = academy_service.get_lesson(lesson_id, tier)
    if not data:
        raise HTTPException(status_code=404, detail="lesson not found")
    response.headers["Cache-Control"] = _PRIVATE_CACHE
    return data


@router.get("/glossary")
async def get_glossary(response: Response):
    response.headers["Cache-Control"] = _PUBLIC_CACHE
    return academy_service.get_glossary()


@router.get("/glossary/search")
async def search_glossary(
    response: Response,
    q: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(8, ge=1, le=20),
):
    response.headers["Cache-Control"] = "private, max-age=60"
    return {"query": q, "results": academy_service.search_glossary(q, limit=limit)}


@router.get("/glossary/{slug}")
async def get_glossary_term(slug: str, response: Response):
    data = academy_service.get_glossary_term(slug)
    if not data:
        raise HTTPException(status_code=404, detail="term not found")
    response.headers["Cache-Control"] = _PUBLIC_CACHE
    return data


@router.get("/scenario-cards")
async def get_scenario_cards(response: Response):
    response.headers["Cache-Control"] = _PUBLIC_CACHE
    return {"cards": academy_service.get_scenario_cards()}


@router.get("/theta-wheel")
async def get_theta_wheel(response: Response):
    response.headers["Cache-Control"] = _PUBLIC_CACHE
    return academy_service.get_theta_wheel_spec()


@router.get("/context")
async def get_live_context_endpoint(response: Response):
    """Faz 1.6 — DVOL (BTC+ETH) + VIX canlı rozet. Fail-soft, kısmi olabilir."""
    from services.academy_context import get_live_context
    data = await get_live_context()
    # 5dk cache eşleşsin — istemci her ders açtığında dövmesin.
    response.headers["Cache-Control"] = "private, max-age=300"
    return data


@router.get("/live-example/{strategy}")
async def get_live_example_endpoint(
    strategy: str,
    response: Response,
    asset: str = Query("BTC", min_length=2, max_length=8),
):
    """'Gerçek Örnek' — bir strateji için bugünün canlı rakamlarıyla somut örnek.

    Deribit (gerçek opsiyon zinciri) birincil; ulaşılamazsa CoinGecko spot +
    Black-Scholes teorik prim (UI'da 'teorik' etiketli). Sayılar deterministik,
    Gemini yok. Fail-soft: available=False döner.
    """
    from services import academy_live_example
    data = await academy_live_example.get_live_example(strategy, asset)
    response.headers["Cache-Control"] = "private, max-age=60"
    return data


# ---------------------------------------------------------------------------
# Progress endpoints (auth required)
# ---------------------------------------------------------------------------


class ProgressRequest(BaseModel):
    lesson_id: str = Field(..., min_length=2, max_length=16)
    # Quiz cevabı opsiyonel — sadece okuma yaparak da tamamlayabilir.
    quiz_choice: Optional[int] = Field(None, ge=0, le=10)


@router.post("/progress")
async def submit_progress(
    body: ProgressRequest,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    quiz_score: Optional[int] = None
    quiz_feedback: Optional[dict] = None

    if body.quiz_choice is not None:
        result = academy_service.evaluate_quiz(body.lesson_id, body.quiz_choice)
        if result is None:
            raise HTTPException(
                status_code=400,
                detail="lesson has no quiz or invalid lesson_id",
            )
        quiz_score = 1 if result.correct else 0
        quiz_feedback = {
            "correct": result.correct,
            "feedback": result.feedback,
            "correct_option_index": result.correct_option_index,
        }

    try:
        progress = await academy_service.record_progress(
            db,
            user_id=current_user.id,
            lesson_id=body.lesson_id,
            quiz_score=quiz_score,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"progress": progress, "quiz": quiz_feedback}


@router.get("/progress")
async def read_progress(
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await academy_service.get_progress(db, user_id=current_user.id)
