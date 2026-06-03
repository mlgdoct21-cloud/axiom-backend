"""News API routes - Retrieval and Filtering"""

import os
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List, Optional
import asyncio

from core.database import get_db
from core.security import get_current_user, get_current_user_id
from schemas.news_schema import NewsResponse, NewsFilter
from schemas.error_schema import ErrorResponse
from services.news import NewsService
from services.event_bus import bus as news_bus, sse_stream
from models.news import NewsItem
from core.logger import get_logger
from models.user import User

logger = get_logger("news_router")

router = APIRouter(
    prefix="/news",
    tags=["news"],
)


# ═══════════════════════════════════════════════════════════════════════════════
# REAL-TIME ENDPOINTS (SSE stream + feed for initial/infinite-scroll load)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/feed",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
)
async def get_news_feed(
    limit: int = 30,
    before_id: Optional[int] = None,
    only_analyzed: bool = False,
    max_age_h: Optional[int] = None,
    symbol: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Dashboard ana feed: en son haberleri döner (analiz durumundan bağımsız).

    - **limit**: Max item count (default 30, max 100)
    - **before_id**: Cursor — bu ID'den eskisini getir (infinite scroll)
    - **only_analyzed**: True ise sadece analizi bitmişler (eski davranış)
    - **max_age_h**: Yaş kesimi saati (varsayılan AXIOM_FEED_MAX_AGE_H env = 2h).
      Bu saatten eski haberler gösterilmez. 0 ile tamamen kapatılır.
    - **symbol**: Verilirse SADECE bu sembolle ilgili haberler. Eşleşme:
      news_items.symbol = X VEYA original_title ICONTAINS X. Sembol filter
      varsa default age cutoff 24 saat (sembol az hareketli olabilir).

    MİMARİ NOTU:
    - DB'ye tüm haberleri yazıyoruz (geriye dönük analiz için gerekli)
    - Feed endpoint'te yaş kesimi: dashboard sadece taze (default 2h) haberi
      göstersin. Eski haberler `?max_age_h=0` ile erişilebilir.
    - FMP/RSS bazen 5-24h eski haberleri "yeni" gibi döndürüyor (yavaş
      indexing). Bu yüzden hem created_at (DB'ye eklenme) hem published_at
      (article'in gerçek yayın zamanı) kontrol edilmeli — burada created_at
      bazlı filtreliyoruz (DB schema'sında published_at henüz yok).
    """
    if limit > 100:
        limit = 100

    # Age cutoff:
    #  - symbol verilmişse default 24h (sembol bazlı geçmiş takip)
    #  - aksi halde AXIOM_FEED_MAX_AGE_H env (default 2h)
    #  - max_age_h query param her ikisini de override eder
    if max_age_h is not None:
        effective_age_h = max_age_h
    elif symbol:
        effective_age_h = 24
    else:
        effective_age_h = int(os.getenv("AXIOM_FEED_MAX_AGE_H", "2"))

    stmt = select(NewsItem)
    if only_analyzed:
        stmt = stmt.where(NewsItem.analyzed == True)  # noqa: E712
    if before_id is not None:
        stmt = stmt.where(NewsItem.id < before_id)
    if effective_age_h > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=effective_age_h)
        stmt = stmt.where(NewsItem.created_at >= cutoff)
    if symbol:
        sym = symbol.upper().strip()
        # symbol kolonu exact OR original_title ICONTAINS (case insensitive)
        from sqlalchemy import or_, func as sqlfunc
        stmt = stmt.where(
            or_(
                sqlfunc.upper(NewsItem.symbol) == sym,
                NewsItem.original_title.ilike(f"%{sym}%"),
            )
        )
    stmt = stmt.order_by(NewsItem.id.desc()).limit(limit)

    try:
        result = await db.execute(stmt)
        items = result.scalars().all()
        return [NewsResponse.from_orm(n) for n in items]
    except Exception as e:
        logger.error(f"Feed endpoint hatası: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news feed"
        )


@router.get("/stream")
async def stream_news(request: Request):
    """
    Server-Sent Events canlı haber akışı.

    Dashboard'a bir kez açılır; crawler yeni bir haberi analiz ettiğinde
    anında `event: news` paketi gönderir. Ayrıca her 15 saniyede heartbeat
    gönderir ki Railway/Cloudflare bağlantıyı kapatmasın.

    Kullanım (frontend):
        const es = new EventSource('/api/v1/news/stream');
        es.addEventListener('news', e => { const n = JSON.parse(e.data); ... });
    """
    q = await news_bus.subscribe()

    async def event_generator():
        try:
            async for chunk in sse_stream(q):
                # Client disconnected?
                if await request.is_disconnected():
                    break
                yield chunk
        except asyncio.CancelledError:
            pass
        finally:
            await news_bus.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx/proxy buffering kapat
            "Connection": "keep-alive",
        },
    )


@router.get(
    "",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_news(
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """
    Get all news with pagination (newest first)

    - **skip**: Number of items to skip (default 0)
    - **limit**: Maximum items to return (default 50, max 100)
    """
    try:
        # Limit maximum results
        if limit > 100:
            limit = 100

        news = await NewsService.get_all_news(db, skip=skip, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except Exception as e:
        logger.error(f"Error getting news: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news"
        )


@router.get(
    "/latest",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_latest_news(
    limit: int = 10,
    db: AsyncSession = Depends(get_db)
):
    """Get latest news items"""
    try:
        if limit > 50:
            limit = 50

        news = await NewsService.get_latest_news(db, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except Exception as e:
        logger.error(f"Error getting latest news: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get latest news"
        )


@router.get(
    "/search",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        400: {"model": ErrorResponse, "description": "Invalid search query"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def search_news(
    q: str,
    skip: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """
    Search news by title, summary, or tags

    - **q**: Search query (required, min 2 characters)
    - **skip**: Number of items to skip
    - **limit**: Maximum items to return
    """
    try:
        if not q or len(q) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Search query must be at least 2 characters"
            )

        if limit > 100:
            limit = 100

        news = await NewsService.search_news(db, q, skip=skip, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error searching news: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to search news"
        )


@router.get(
    "/source/{source}",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_news_by_source(
    source: str,
    skip: int = 0,
    limit: int = 50,
    
    db: AsyncSession = Depends(get_db)
):
    """
    Get news from a specific source

    - **source**: Source name (e.g., "Bloomberg", "Yahoo Finance")
    - **skip**: Number of items to skip
    - **limit**: Maximum items to return
    """
    try:
        if limit > 100:
            limit = 100

        news = await NewsService.get_news_by_source(db, source, skip=skip, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except Exception as e:
        logger.error(f"Error getting news by source: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news by source"
        )


@router.get(
    "/tag/{tag}",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_news_by_tag(
    tag: str,
    skip: int = 0,
    limit: int = 50,
    
    db: AsyncSession = Depends(get_db)
):
    """
    Get news by tag (AI-generated tags)

    - **tag**: Tag to search for
    - **skip**: Number of items to skip
    - **limit**: Maximum items to return
    """
    try:
        if not tag or len(tag) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Tag must be at least 2 characters"
            )

        if limit > 100:
            limit = 100

        news = await NewsService.get_news_by_tag(db, tag, skip=skip, limit=limit)
        return [NewsResponse.from_orm(n) for n in news]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting news by tag: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news by tag"
        )


@router.post(
    "/filter",
    response_model=List[NewsResponse],
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def filter_news(
    filters: NewsFilter,
    
    db: AsyncSession = Depends(get_db)
):
    """
    Advanced news filtering

    Request body:
    - **source**: Filter by source (optional)
    - **tag**: Filter by AI tag (optional)
    - **search**: Full-text search (optional)
    - **skip**: Pagination offset
    - **limit**: Maximum results
    """
    try:
        # Validate limit
        if filters.limit > 100:
            filters.limit = 100

        news = await NewsService.filter_news(db, filters)
        return [NewsResponse.from_orm(n) for n in news]
    except Exception as e:
        logger.error(f"Error filtering news: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to filter news"
        )


@router.get(
    "/{news_id}",
    response_model=NewsResponse,
    status_code=status.HTTP_200_OK,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "News not found"},
        500: {"model": ErrorResponse, "description": "Server error"}
    }
)
async def get_news_by_id(
    news_id: int,

    db: AsyncSession = Depends(get_db)
):
    """Get specific news item by ID — lazy AI-fill if missing.

    Lean v3: crawler ham haber DB'ye yazar; AI özet+yorum kullanıcı haberi
    açtığında üretilir + DB'ye cache'lenir. İkinci açılışta Gemini çağrısı
    YOK. Batch loop hâlâ açıksa zaten dolu gelir, no-op.
    """
    try:
        news = await NewsService.get_news_by_id(db, news_id)

        if not news:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="News item not found"
            )

        # Lazy fill: dashboard_summary VEYA axiom_analysis eksikse on-demand üret
        needs_fill = not (news.dashboard_summary and news.axiom_analysis)
        if needs_fill:
            try:
                from services.ai_service import generate_summary
                result = await generate_summary(
                    news_title=news.original_title,
                    news_link=news.original_link,
                )
                tg_hook = (result or {}).get("telegram_hook")
                dash = (result or {}).get("dashboard_summary")
                axiom = (result or {}).get("axiom_analysis")
                if dash or axiom or tg_hook:
                    from sqlalchemy import update
                    await db.execute(
                        update(NewsItem)
                        .where(NewsItem.id == news_id)
                        .values(
                            telegram_hook=tg_hook or news.telegram_hook,
                            dashboard_summary=dash or news.dashboard_summary,
                            axiom_analysis=axiom or news.axiom_analysis,
                            ai_summary=dash or news.ai_summary,
                            analyzed=True,
                        )
                    )
                    await db.commit()
                    # Re-fetch enriched row
                    news = await NewsService.get_news_by_id(db, news_id)
            except Exception as fill_err:
                # Lazy-fill başarısız olursa ham haberi yine de döndür
                logger.warning(f"Lazy AI fill failed for news #{news_id}: {fill_err}")

        return NewsResponse.from_orm(news)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting news by ID: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to get news item"
        )
