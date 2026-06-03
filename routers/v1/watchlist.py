"""Watchlist router — kişisel takip listesi CRUD + diff feed.

GET    /watchlist            → kullanıcının listesi + her sembolün son dossier özeti
POST   /watchlist            → sembol ekle (category, opsiyonel avg_cost/qty/notes)
PATCH  /watchlist/{symbol}   → kategori veya cost/qty/notes güncelle
DELETE /watchlist/{symbol}   → sembol çıkar
POST   /watchlist/{symbol}/refresh → ANINDA dossier yenile + diff hesapla
GET    /watchlist/diffs      → kullanıcının son diff'leri (since=ISO opsiyonel)

Auth: get_current_user zorunlu (kişisel araç).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from core.database import AsyncSessionLocal
from core.logger import get_logger
from core.rate_limit import limiter
from core.security import get_current_user
from models.user import User

logger = get_logger("watchlist_router")

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


_VALID_CATEGORIES = {"long_term", "swing"}


class WatchlistCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=16)
    category: Literal["long_term", "swing"]
    avg_cost: Optional[float] = None
    qty: Optional[float] = None
    notes: Optional[str] = Field(None, max_length=500)


class WatchlistUpdate(BaseModel):
    category: Optional[Literal["long_term", "swing"]] = None
    avg_cost: Optional[float] = None
    qty: Optional[float] = None
    notes: Optional[str] = Field(None, max_length=500)


def _norm_symbol(symbol: str) -> str:
    sym = symbol.upper().strip()
    if not sym.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Geçersiz sembol formatı")
    if len(sym) > 16:
        raise HTTPException(status_code=400, detail="Sembol uzun")
    return sym


@router.get("")
async def list_watchlist(user: User = Depends(get_current_user)) -> dict:
    """Kullanıcının watchlist'i + her sembol için son dossier özeti + son diff."""
    async with AsyncSessionLocal() as db:
        items_sql = text("""
            SELECT id, symbol, category, avg_cost, qty, notes,
                   last_dossier_at, last_trigger_check_at, created_at
            FROM watchlist_items
            WHERE user_id = :uid
            ORDER BY category, symbol
        """)
        res = await db.execute(items_sql, {"uid": user.id})
        rows = res.fetchall()

        out_items = []
        for r in rows:
            sym = r[1]
            # Son dossier özeti
            d_sql = text("""
                SELECT id, payload, created_at FROM trade_dossiers
                WHERE symbol = :sym ORDER BY id DESC LIMIT 1
            """)
            dres = await db.execute(d_sql, {"sym": sym})
            drow = dres.fetchone()
            dossier_summary = None
            if drow:
                p = drow[1] or {}
                dossier_summary = {
                    "id": int(drow[0]),
                    "verdict": p.get("verdict"),
                    "conviction": p.get("conviction"),
                    "thesis": (p.get("thesis") or "")[:240],
                    "last_close": ((p.get("_data") or {}).get("ta") or {}).get("last_close"),
                    "created_at": drow[2].isoformat() if drow[2] else None,
                }
            # Son diff
            diff_sql = text("""
                SELECT diff_type, severity, summary, created_at
                FROM dossier_diffs
                WHERE user_id = :uid AND symbol = :sym
                ORDER BY id DESC LIMIT 1
            """)
            difres = await db.execute(diff_sql, {"uid": user.id, "sym": sym})
            difrow = difres.fetchone()
            last_diff = None
            if difrow:
                last_diff = {
                    "diff_type": difrow[0],
                    "severity": difrow[1],
                    "summary": difrow[2],
                    "created_at": difrow[3].isoformat() if difrow[3] else None,
                }

            out_items.append({
                "id": int(r[0]),
                "symbol": sym,
                "category": r[2],
                "avg_cost": float(r[3]) if r[3] is not None else None,
                "qty": float(r[4]) if r[4] is not None else None,
                "notes": r[5],
                "last_dossier_at": r[6].isoformat() if r[6] else None,
                "last_trigger_check_at": r[7].isoformat() if r[7] else None,
                "created_at": r[8].isoformat() if r[8] else None,
                "dossier": dossier_summary,
                "last_diff": last_diff,
            })

    return {"count": len(out_items), "items": out_items}


@router.post("", status_code=201)
@limiter.limit("20/minute")
async def add_to_watchlist(
    request: Request,
    body: WatchlistCreate,
    user: User = Depends(get_current_user),
) -> dict:
    sym = _norm_symbol(body.symbol)
    if body.category not in _VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail="category long_term|swing olmalı")

    async with AsyncSessionLocal() as db:
        # Çift kontrol
        chk = await db.execute(
            text("SELECT id FROM watchlist_items WHERE user_id = :uid AND symbol = :sym"),
            {"uid": user.id, "sym": sym},
        )
        if chk.fetchone():
            raise HTTPException(status_code=409, detail="Bu sembol zaten watchlist'te")

        ins = text("""
            INSERT INTO watchlist_items
                (user_id, symbol, category, avg_cost, qty, notes, created_at)
            VALUES (:uid, :sym, :cat, :ac, :qt, :nt, NOW())
            RETURNING id, created_at
        """)
        res = await db.execute(ins, {
            "uid": user.id, "sym": sym, "cat": body.category,
            "ac": body.avg_cost, "qt": body.qty, "nt": body.notes,
        })
        row = res.fetchone()
        await db.commit()

    logger.info(f"watchlist add: user={user.id} symbol={sym} category={body.category}")
    return {
        "id": int(row[0]),
        "symbol": sym,
        "category": body.category,
        "created_at": row[1].isoformat() if row[1] else None,
    }


@router.patch("/{symbol}")
async def update_watchlist(
    body: WatchlistUpdate,
    symbol: str = Path(..., min_length=1, max_length=16),
    user: User = Depends(get_current_user),
) -> dict:
    sym = _norm_symbol(symbol)
    fields, params = [], {"uid": user.id, "sym": sym}
    if body.category is not None:
        if body.category not in _VALID_CATEGORIES:
            raise HTTPException(status_code=400, detail="category long_term|swing olmalı")
        fields.append("category = :cat")
        params["cat"] = body.category
    if body.avg_cost is not None:
        fields.append("avg_cost = :ac")
        params["ac"] = body.avg_cost
    if body.qty is not None:
        fields.append("qty = :qt")
        params["qt"] = body.qty
    if body.notes is not None:
        fields.append("notes = :nt")
        params["nt"] = body.notes
    if not fields:
        raise HTTPException(status_code=400, detail="Güncellenecek alan yok")

    sql = text(f"""
        UPDATE watchlist_items SET {', '.join(fields)}
        WHERE user_id = :uid AND symbol = :sym
        RETURNING id
    """)
    async with AsyncSessionLocal() as db:
        res = await db.execute(sql, params)
        row = res.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bu sembol watchlist'te yok")
        await db.commit()
    return {"ok": True, "id": int(row[0])}


@router.delete("/{symbol}")
async def remove_from_watchlist(
    symbol: str = Path(..., min_length=1, max_length=16),
    user: User = Depends(get_current_user),
) -> dict:
    sym = _norm_symbol(symbol)
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text("DELETE FROM watchlist_items WHERE user_id = :uid AND symbol = :sym RETURNING id"),
            {"uid": user.id, "sym": sym},
        )
        row = res.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bu sembol watchlist'te yok")
        await db.commit()
    logger.info(f"watchlist remove: user={user.id} symbol={sym}")
    return {"ok": True}


@router.post("/{symbol}/refresh")
@limiter.limit("6/minute")
async def refresh_watchlist_item(
    request: Request,
    symbol: str = Path(..., min_length=1, max_length=16),
    user: User = Depends(get_current_user),
) -> dict:
    """Anında dossier yenile + diff hesapla (kullanıcı dashboard'tan elle tetikler)."""
    sym = _norm_symbol(symbol)
    # Watchlist'te olduğunu doğrula
    async with AsyncSessionLocal() as db:
        chk = await db.execute(
            text("SELECT category, last_dossier_at FROM watchlist_items WHERE user_id = :uid AND symbol = :sym"),
            {"uid": user.id, "sym": sym},
        )
        row = chk.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bu sembol watchlist'te yok")
        category = row[0]
        last_dossier_at = row[1]

    # Supervisor helper'ı kullan (DRY)
    from services.watchlist_supervisor import _process_item
    item = {
        "user_id": user.id,
        "symbol": sym,
        "category": category,
        # last_dossier_at = epoch ki refresh tetiklensin
        "last_dossier_at": None,
        "last_trigger_check_at": None,
    }
    await _process_item(item, datetime.now(timezone.utc))
    return {"ok": True, "symbol": sym, "category": category}


@router.get("/diffs")
async def list_diffs(
    since: Optional[str] = Query(None, description="ISO datetime — bu tarihten sonraki diff'ler"),
    limit: int = Query(50, ge=1, le=200),
    severity: Optional[Literal["low", "mid", "high"]] = Query(None),
    user: User = Depends(get_current_user),
) -> dict:
    where = ["user_id = :uid"]
    params: dict = {"uid": user.id, "lim": limit}
    if since:
        where.append("created_at >= :since")
        try:
            params["since"] = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="since ISO datetime olmalı")
    if severity:
        where.append("severity = :sv")
        params["sv"] = severity

    sql = text(f"""
        SELECT id, symbol, prev_snapshot_id, curr_snapshot_id, diff_type, severity,
               summary, details, sent_at, created_at
        FROM dossier_diffs
        WHERE {' AND '.join(where)}
        ORDER BY id DESC LIMIT :lim
    """)
    async with AsyncSessionLocal() as db:
        res = await db.execute(sql, params)
        rows = res.fetchall()

    import json as _json
    out = []
    for r in rows:
        details = None
        if r[7]:
            try:
                details = _json.loads(r[7])
            except Exception:
                details = {"raw": r[7][:300]}
        out.append({
            "id": int(r[0]),
            "symbol": r[1],
            "prev_snapshot_id": int(r[2]) if r[2] else None,
            "curr_snapshot_id": int(r[3]) if r[3] else None,
            "diff_type": r[4],
            "severity": r[5],
            "summary": r[6],
            "details": details,
            "sent_at": r[8].isoformat() if r[8] else None,
            "created_at": r[9].isoformat() if r[9] else None,
        })
    return {"count": len(out), "items": out}
