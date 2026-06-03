"""Trade Dossier router — sembol başına AL/TUT/SAT sentezi.

POST /api/v1/dossier/{symbol}?refresh=0
  → Cache (30dk) hit ise döner; aksi halde TA+Temel+Haber+Makro topla,
    Gemini 2.5 Flash ile sentezle, DB'ye yaz, döndür.

Auth: kişisel kullanım (Lean v3 Faz 3) — `get_current_user` zorunlu.
Rate-limit: 10/dakika (Gemini cost guard).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.logger import get_logger
from core.rate_limit import limiter
from core.security import get_current_user
from models.user import User
from services.dossier_service import get_or_build_dossier

logger = get_logger("dossier_router")

router = APIRouter(prefix="/dossier", tags=["dossier"])


@router.post("/{symbol}")
@limiter.limit("10/minute")
async def create_dossier(
    request: Request,
    symbol: str = Path(..., min_length=1, max_length=16, description="Sembol (BIST/CRYPTO/US)"),
    refresh: int = Query(0, ge=0, le=1, description="1: cache'i atla, yeniden üret"),
    user: User = Depends(get_current_user),
) -> dict:
    """Trade dossier üret veya 30dk cache'ten döndür."""
    sym = symbol.upper().strip()
    if not sym.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Geçersiz sembol formatı")
    try:
        result = await get_or_build_dossier(sym, force_refresh=bool(refresh))
        return result
    except Exception as e:
        logger.error(f"Dossier üretim genel hata {sym}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Dossier üretilemedi: {e}")


@router.get("/{symbol}/latest")
async def get_latest_dossier(
    symbol: str = Path(..., min_length=1, max_length=16),
    user: User = Depends(get_current_user),
) -> dict:
    """En son üretilmiş dossier'ı döner (cache TTL kontrolü YOK — geçmiş kayıt)."""
    sym = symbol.upper().strip()
    if not sym.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Geçersiz sembol formatı")
    from sqlalchemy import text
    from core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        sql = text("""
            SELECT symbol, symbol_type, payload, model_used, created_at, error
            FROM trade_dossiers
            WHERE symbol = :sym
            ORDER BY created_at DESC LIMIT 1
        """)
        res = await db.execute(sql, {"sym": sym})
        row = res.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bu sembol için henüz dossier yok")
        return {
            "symbol": row[0],
            "symbol_type": row[1],
            "payload": row[2],
            "model_used": row[3],
            "created_at": row[4].isoformat() if row[4] else None,
            "error": row[5],
            "from_cache": True,
        }
